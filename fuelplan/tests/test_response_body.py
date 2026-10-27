"""Spec 01 TC-20, 21, 22, 23, 25 and 45: the 200 body, its money and its station data.

TC-45 compares the whole body with the §7a example. TC-20/21/22 run the recorded NY→LA
route against stations imported by `import_fuel_stations` from real rows of the provided
CSV, placed along that route; TC-21 then re-derives each stop's price from the raw CSV.
"""

import csv
import json
import shutil
import tempfile
from decimal import ROUND_HALF_UP, Decimal
from io import StringIO
from pathlib import Path

from django.core.management import call_command

from fuelplan.fuel_csv import parse_row
from fuelplan.geo import RouteCorridor, build_route_line, miles_from_meters
from fuelplan.geocoding import LOCATION_FILE_COLUMNS
from fuelplan.optimizer import Candidate, FuelPlan, plan_fuel_stops
from fuelplan.regions import LOWER_48_AND_DC
from fuelplan.serializers import (
    arrival_text,
    gallons_text,
    gap_leg_text,
    miles_text,
    money_text,
    snap_miles,
)
from fuelplan.stations import StationIndex

from .base import ApiTestCase
from .planner_harness import (
    FIXTURES,
    TEST_PLACES,
    PlannerHarness,
    json_body,
    meridian_point,
    meridian_reply,
    meridian_request,
    osrm_fixture,
    station,
    synthetic_index,
)

RAW_CSV = Path(__file__).resolve().parents[2] / "data" / "raw" / "fuel-prices.csv"
CENT = Decimal("0.01")
TENTH = Decimal("0.1")

# The §7a success example, verbatim (spec 01, amended 2026-10-28: 7 notes, `matched`).
GOLDEN_BODY = json.loads((FIXTURES / "golden_chicago_denver.json").read_text(encoding="utf-8"))
GOLDEN_REQUEST = {
    "start": {"city": "Chicago", "state": "IL"},
    "finish": {"city": "Denver", "state": "CO"},
    "current_fuel_gallons": 50,
    "reserve_gallons": 5,
}
# Points that project onto the chicago_denver fixture at (mile 412.60, offset 0.60) and
# (844.60, 0.40); the test asserts this first, so the golden body depends on nothing else.
GOLDEN_STATION_POINTS = {4021: (-94.911324, 41.321077), 7310: (-102.287757, 40.185483)}


def golden_index() -> StationIndex:
    """The two §7a stations, at points that give the §7a miles and detours."""
    return synthetic_index(
        [
            station(
                4021,
                point=GOLDEN_STATION_POINTS[4021],
                price="3.199",
                name="EXAMPLE TRAVEL CENTER #1",
                address="I-80, EXIT 46",
                city="Walnut",
                state="IA",
            ),
            station(
                7310,
                point=GOLDEN_STATION_POINTS[7310],
                price="3.459",
                quality="address_match",
                name="EXAMPLE TRAVEL CENTER #2",
                address="I-76, EXIT 125",
                city="Sterling",
                state="CO",
            ),
        ]
    )


def _without_illustrative_fields(body: dict) -> dict:
    """§7a marks these as illustrative (TC-45): ids, links, version, geometry, stop points."""
    body = json.loads(json.dumps(body))
    for key in ("plan_id", "map_url"):
        body.pop(key)
    body["assumptions"].pop("dataset_version")
    body["route"].pop("geometry")
    for stop in body["fuel_stops"]:
        stop.pop("location")
    return body


class GoldenBodyTests(ApiTestCase):
    """TC-45 (FR-19, FR-24–26, FR-30): the §7a example reproduces exactly."""

    def setUp(self) -> None:
        super().setUp()
        self.harness = PlannerHarness(index=golden_index())
        patcher = self.harness.patch_view()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_fixture_stations_project_to_the_spec_miles_and_detours(self) -> None:
        reply = json.loads(osrm_fixture("chicago_denver"))
        points = [tuple(p) for p in reply["routes"][0]["geometry"]["coordinates"]]
        line = build_route_line(
            points, length_miles=miles_from_meters(reply["routes"][0]["distance"])
        )
        candidates = self.harness.index.candidates_on(RouteCorridor(line))

        self.assertEqual(
            [(c.station_id, c.route_mile, c.detour_miles) for c in candidates],
            [
                (4021, Decimal("412.60"), Decimal("1.20")),
                (7310, Decimal("844.60"), Decimal("0.80")),
            ],
        )

    def test_golden_chicago_denver(self) -> None:
        self.harness.queue_fixture("chicago_denver")

        response = self.post_json(GOLDEN_REQUEST)

        self.assertEqual(response.status_code, 200, response.content)
        body = json_body(response)
        self.assertEqual(
            _without_illustrative_fields(body), _without_illustrative_fields(GOLDEN_BODY)
        )
        self.assertEqual(body["route"]["geometry"], GOLDEN_BODY["route"]["geometry"])
        self.assertEqual(body["assumptions"]["dataset_version"], self.harness.index.dataset_version)
        self.assertEqual(self.harness.transport.count, 1)

    def test_the_total_is_rounded_once_not_summed_from_the_shown_lines(self) -> None:
        self.harness.queue_fixture("chicago_denver")

        body = json_body(self.post_json(GOLDEN_REQUEST))

        shown_lines = sum(Decimal(stop["line_cost"]) for stop in body["fuel_stops"])
        self.assertEqual(shown_lines, Decimal("183.05"))
        self.assertEqual(body["totals"]["total_purchase_cost"], "183.04")


class MoneyRoundingTests(ApiTestCase):
    """TC-25 (FR-30, NFR-5, I4): HALF_UP cents, a total rounded once, Decimal only."""

    def test_money_rounding_and_types(self) -> None:
        rows = {
            "41 x 3.199": (41 * Decimal("3.199"), "131.16"),
            "15 x 3.459 (HALF_EVEN would give 51.88)": (15 * Decimal("3.459"), "51.89"),
            "golden total 183.044": (Decimal("183.044"), "183.04"),
            "7 x 3.215 = 22.505": (7 * Decimal("3.215"), "22.51"),
            "1 x 2.345 (kills HALF_EVEN)": (Decimal("2.345"), "2.35"),
        }
        for name, (amount, expected) in rows.items():
            with self.subTest(row=name):
                self.assertEqual(money_text(amount), expected)

    def test_a_seven_stop_total_differs_from_the_shown_lines_by_at_most_half_a_cent_each(
        self,
    ) -> None:
        lines = [1 * Decimal("3.105")] * 7

        total = Decimal(money_text(sum(lines)))
        shown_sum = sum(Decimal(money_text(line)) for line in lines)

        self.assertEqual((str(total), str(shown_sum)), ("21.74", "21.77"))
        self.assertLessEqual(abs(shown_sum - total), Decimal("0.035"))

    def test_gallons_and_miles_round_the_way_the_spec_says(self) -> None:
        self.assertEqual(arrival_text(Decimal("5.09")), "5.0")  # arrivals round down
        self.assertEqual(arrival_text(Decimal("8.68")), "8.6")
        self.assertEqual(gallons_text(Decimal("1005.5") / 10), "100.6")  # burned: HALF_UP
        self.assertEqual(miles_text(Decimal("412.65")), "412.7")
        self.assertEqual(gap_leg_text(Decimal("450.01")), "450.1")  # gap leg rounds up

    def test_every_money_value_reaching_the_serializer_is_a_decimal(self) -> None:
        candidates = [
            Candidate(4021, Decimal("412.60"), Decimal("1.20"), Decimal("3.199"), False),
            Candidate(7310, Decimal("844.60"), Decimal("0.80"), Decimal("3.459"), False),
        ]

        plan = plan_fuel_stops(
            route_miles=Decimal("1003.20"),
            candidates=candidates,
            start_gallons=Decimal("50.0"),
            reserve_gallons=Decimal("5.0"),
        )

        self.assertIsInstance(plan, FuelPlan)
        self.assertIsInstance(plan.total_cost, Decimal)
        for stop in plan.stops:
            self.assertIsInstance(stop.line_cost, Decimal)
            self.assertIsInstance(stop.candidate.price_per_gallon, Decimal)


class WarningTests(ApiTestCase):
    """TC-23 (FR-27, FR-28, FR-42): verification flags, approximate stops and far snaps."""

    def setUp(self) -> None:
        super().setUp()
        cheap_approximate = station(
            1, point=meridian_point(300.0), price="2.000", quality="approximate", city="Midway"
        )
        dear_approximate = station(
            2, point=meridian_point(100.0), price="4.000", quality="approximate"
        )
        self.harness = PlannerHarness(index=synthetic_index([cheap_approximate, dear_approximate]))
        patcher = self.harness.patch_view()
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, *, snap_m: tuple[float, float] | None, **fuel: object) -> dict:
        """POST the 600-mile trip; `snap_m=None` expects the route from the cache."""
        if snap_m is not None:
            self.harness.queue(meridian_reply(600.0, snap_m=snap_m))
        response = self.post_json(meridian_request(600.0, **fuel), keep_cache=snap_m is None)
        self.assertEqual(response.status_code, 200, response.content)
        return json_body(response)

    def test_only_the_chosen_approximate_stop_and_the_far_start_are_warned_about(self) -> None:
        body = self._post(snap_m=(1931.21, 1609.344))  # 1.2 mi and exactly 1.0 mi

        self.assertEqual([stop["station_id"] for stop in body["fuel_stops"]], ["1"])
        self.assertEqual(
            body["warnings"],
            [
                {
                    "code": "far_snap",
                    "message": "The start point is 1.2 mi from the nearest road; the route "
                    "starts there, and those miles are not counted.",
                    "field": "start",
                    "snap_miles": "1.2",
                },
                {
                    "code": "approximate_location",
                    "message": "Station 1 (TEST STATION 1, Midway TX) has only a town-level "
                    "location; its real position and detour may differ.",
                    "station_id": "1",
                },
            ],
        )
        self.assertFalse(body["detours_verified"])
        self.assertFalse(body["fuel_stops"][0]["detour_verified"])
        self.assertEqual(body["assumptions"]["detour_method"], "straight_line_estimate")

    def test_a_snap_warns_only_when_its_shown_value_is_above_1_0_mile(self) -> None:
        rows = {1609.344: None, 1689.7: None, 1689.9: "1.1", 1931.21: "1.2"}
        for snap_m, shown in rows.items():
            with self.subTest(snap_m=snap_m):
                body = self._post(snap_m=(0.0, snap_m))

                far_snaps = [w for w in body["warnings"] if w["code"] == "far_snap"]
                expected = [] if shown is None else [("finish", shown)]
                self.assertEqual([(w["field"], w["snap_miles"]) for w in far_snaps], expected)

    def test_a_far_snap_is_still_warned_about_on_a_route_cache_hit(self) -> None:
        self._post(snap_m=(1931.21, 0.0))

        body = self._post(snap_m=None, current_fuel_gallons=40)  # same ends, cached route

        self.assertEqual(
            body["meta"], {"routing_calls": 0, "route_cache": "hit", "plan_cache": "miss"}
        )
        far_snaps = [w for w in body["warnings"] if w["code"] == "far_snap"]
        self.assertEqual([(w["field"], w["snap_miles"]) for w in far_snaps], [("start", "1.2")])

    def test_snap_miles_are_rounded_once_from_the_raw_metres(self) -> None:
        self.assertEqual(str(snap_miles(1689.7)), "1.0")  # 1.0499… mi
        self.assertEqual(str(snap_miles(1931.21)), "1.2")


def _ny_la_route() -> tuple[list[tuple[float, float]], float]:
    route = json.loads(osrm_fixture("ny_la"))["routes"][0]
    return [tuple(p) for p in route["geometry"]["coordinates"]], route["distance"]


def _raw_csv_rows() -> list[dict[str, str]]:
    with RAW_CSV.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


NY_LA_STATION_MILES = [107.0 * k for k in range(1, 27)]
NY_LA_OFF_ROUTE = {3, 9, 15, 21}  # these stations sit 0.02° north of the line (d ≈ 1–3 mi)
NY_LA_MULTI_PRICE_IDS = (105, 128)  # several US rows with different prices


def _chosen_ids(rows: list[dict[str, str]]) -> list[int]:
    """26 OPIS IDs with a valid lower-48 row: two multi-price IDs, then the lowest IDs."""
    valid = sorted(
        {
            parsed.opis_id
            for parsed in (parse_row(row) for row in rows)
            if parsed is not None and parsed.state in LOWER_48_AND_DC
        }
    )
    others = [opis_id for opis_id in valid if opis_id not in NY_LA_MULTI_PRICE_IDS]
    return [*NY_LA_MULTI_PRICE_IDS, *others[: len(NY_LA_STATION_MILES) - 2]]


def import_stations_along_ny_la(workdir: Path) -> None:
    """Run `import_fuel_stations` on real CSV rows placed every 107 mi along NY→LA."""
    points, distance_m = _ny_la_route()
    line = build_route_line(points, length_miles=miles_from_meters(distance_m))
    rows = _raw_csv_rows()
    chosen = _chosen_ids(rows)
    csv_path, locations_path = workdir / "stations.csv", workdir / "locations.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(
            row for row in rows if row["OPIS Truckstop ID"].strip() in map(str, chosen)
        )
    with locations_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOCATION_FILE_COLUMNS)
        writer.writeheader()
        for number, (opis_id, mile) in enumerate(zip(chosen, NY_LA_STATION_MILES, strict=True)):
            index = next(i for i, m in enumerate(line.cumulative_miles) if m >= mile)
            lon, lat = points[index]
            lat += 0.02 if number in NY_LA_OFF_ROUTE else 0.0
            keys = {
                parsed.address_key
                for parsed in (parse_row(row) for row in rows)
                if parsed is not None and parsed.opis_id == opis_id
            }
            for key in sorted(keys):
                writer.writerow(
                    {
                        "address_key": key,
                        "lat": f"{lat:.6f}",
                        "lon": f"{lon:.6f}",
                        "result_type": "amenity",
                        "confidence": "1.0",
                        "state_code": key.rsplit("|", 1)[1],
                        "source": "geoapify",
                        "attribution": "© OpenStreetMap contributors",
                        "license": "Open Database License",
                        "source_url": "https://www.openstreetmap.org/copyright",
                    }
                )
    call_command(
        "import_fuel_stations",
        "--csv",
        str(csv_path),
        "--locations",
        str(locations_path),
        "--places",
        str(TEST_PLACES),
        stdout=StringIO(),
    )


FR24_FIELDS = {
    "plan_id",
    "map_url",
    "start",
    "finish",
    "route",
    "fuel_stops",
    "finish_fuel_on_arrival_gallons",
    "totals",
    "assumptions",
    "detours_verified",
    "notes",
    "warnings",
    "meta",
}
FR25_FIELDS = {
    "sequence",
    "station_id",
    "name",
    "address",
    "city",
    "state",
    "location",
    "route_mile",
    "estimated_detour_miles",
    "fuel_on_arrival_gallons",
    "purchase_rule",
    "gallons_to_buy",
    "price_per_gallon",
    "line_cost",
    "miles_to_next",
    "location_quality",
    "detour_verified",
}
INTEGER_FIELDS = {"sequence", "gallons_to_buy", "gallons_bought", "routing_calls"}


def _numbers_outside_coordinates(value: object, key: str = "") -> list[tuple[str, object]]:
    """Every (key, number) pair except GeoJSON positions and the echoed input (FR-7)."""
    if key in ("coordinates", "input"):
        return []
    if isinstance(value, dict):
        return [pair for k, v in value.items() for pair in _numbers_outside_coordinates(v, k)]
    if isinstance(value, list):
        return [pair for item in value for pair in _numbers_outside_coordinates(item, key)]
    if isinstance(value, int | float) and not isinstance(value, bool):
        return [(key, value)]
    return []


class NewYorkLosAngelesTests(ApiTestCase):
    """TC-20, TC-21, TC-22 on the recorded NY→LA route (34,664 points, 2,794.2 mi)."""

    @classmethod
    def setUpTestData(cls) -> None:
        workdir = Path(tempfile.mkdtemp())
        try:
            import_stations_along_ny_la(workdir)
        finally:
            shutil.rmtree(workdir)

    def setUp(self) -> None:
        super().setUp()
        self.harness = PlannerHarness(index=StationIndex.from_database())
        patcher = self.harness.patch_view()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.harness.queue_fixture("ny_la")
        response = self.post_json(
            {
                "start": {"city": "New York", "state": "NY"},
                "finish": {"latitude": 34.05223, "longitude": -118.24368},
            }
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.body = json_body(response)
        self.stops = self.body["fuel_stops"]

    def test_happy_path_ny_la(self) -> None:
        # TC-20: every field, its type, and the arithmetic that ties them together.
        body, stops = self.body, self.stops
        self.assertEqual(set(body), FR24_FIELDS)
        self.assertGreaterEqual(len(stops), 5)
        for stop in stops:
            self.assertEqual(set(stop), FR25_FIELDS)
            self.assertIsInstance(stop["station_id"], str)
            self.assertIn(stop["purchase_rule"], {"fill_tank", "to_next_stop", "to_finish"})
            # AC-17 on the shown values: arrival (rounded down) + bought never passes 50.
            self.assertLessEqual(
                Decimal(stop["fuel_on_arrival_gallons"]) + stop["gallons_to_buy"], 50
            )
        numbers = _numbers_outside_coordinates(body)
        self.assertTrue(all(isinstance(value, int) for _, value in numbers), numbers)
        self.assertTrue({key for key, _ in numbers} <= INTEGER_FIELDS, numbers)

        totals = body["totals"]
        exact_total = sum(
            stop["gallons_to_buy"] * Decimal(stop["price_per_gallon"]) for stop in stops
        )
        self.assertEqual(
            totals["total_purchase_cost"], str(exact_total.quantize(CENT, ROUND_HALF_UP))
        )
        self.assertEqual(totals["gallons_bought"], sum(stop["gallons_to_buy"] for stop in stops))
        driven = Decimal(body["route"]["driven_distance_miles"])
        self.assertLessEqual(abs(Decimal(totals["fuel_burned_gallons"]) - driven / 10), TENTH)

        first = stops[0]
        start_leg = Decimal(first["route_mile"]) + Decimal(first["estimated_detour_miles"]) / 2
        legs = start_leg + sum(Decimal(stop["miles_to_next"]) for stop in stops)
        self.assertLessEqual(abs(legs - driven), (len(stops) + Decimal("2.5")) * Decimal("0.05"))

        # Each shown arrival is rounded down (< 0.1 below the real tank), and so is the
        # previous one this check starts from; the two errors partly cancel, so their
        # difference is under 0.1. Shown legs are 0.1 HALF_UP (start leg: route mile + d/2,
        # < 0.075 mi off), adding < 0.0075 gal. Hence the bound 0.1 + 0.0075.
        arrival_tolerance = Decimal("0.1075")
        tank = Decimal("50.0")
        arrivals = [Decimal(s["fuel_on_arrival_gallons"]) for s in stops]
        arrivals.append(Decimal(body["finish_fuel_on_arrival_gallons"]))
        leg_miles = [start_leg] + [Decimal(stop["miles_to_next"]) for stop in stops]
        for number, (arrival, leg) in enumerate(zip(arrivals, leg_miles, strict=True)):
            self.assertLess(abs(arrival - (tank - leg / 10)), arrival_tolerance, number)
            if number < len(stops):
                tank = arrival + stops[number]["gallons_to_buy"]
        self.assertEqual(
            body["meta"], {"routing_calls": 1, "route_cache": "miss", "plan_cache": "miss"}
        )
        self.assertEqual(self.harness.transport.count, 1)

    def test_stops_from_csv_only(self) -> None:
        # TC-21: each stop is a US row of the provided CSV, at that ID's lowest valid price.
        rows = _raw_csv_rows()
        for stop in self.stops:
            with self.subTest(station_id=stop["station_id"]):
                us_rows = [
                    row
                    for row in rows
                    if row["OPIS Truckstop ID"].strip() == stop["station_id"]
                    and row["State"].strip().upper() in LOWER_48_AND_DC
                ]
                prices = []
                for row in us_rows:
                    text = row["Retail Price"].strip()
                    if text and 0 < Decimal(text) < 100:
                        prices.append(text)
                self.assertTrue(prices)
                # min() keeps the first of equal values, as the import does on a tie.
                self.assertEqual(stop["price_per_gallon"], min(prices, key=Decimal))

    def test_the_response_line_is_under_2000_positions_with_the_osrm_ends_kept(self) -> None:
        # TC-22 on the recorded route
        points, _ = _ny_la_route()
        coordinates = self.body["route"]["geometry"]["coordinates"]

        self.assertLess(len(coordinates), 2_000)
        self.assertEqual(coordinates[0], [round(points[0][0], 5), round(points[0][1], 5)])
        self.assertEqual(coordinates[-1], [round(points[-1][0], 5), round(points[-1][1], 5)])
