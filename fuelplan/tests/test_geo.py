"""Spec 01 TC-14 (FR-18), TC-22 (NFR-6) and the geo half of TC-41 (NFR-13).

Pure geometry in `fuelplan/geo.py`. Every route is built in the test from constants or
a seeded random generator; nothing is read from disk except geo.py's own source.
TC-14's last clause ("the leg includes d/2 each side") is the optimiser's, tested in 2A.
"""

import ast
import math
import random
from decimal import Decimal
from pathlib import Path

from fuelplan import geo
from fuelplan.geo import (
    CORRIDOR_PAD_MILES,
    MAX_DETOUR_MILES,
    MILE_STEP,
    MILES_PER_DEGREE_LATITUDE,
    RESPONSE_MAX_POSITIONS,
    RESPONSE_TOLERANCE_MILES,
    ROUTE_TOLERANCE_MILES,
    LonLat,
    RouteCorridor,
    RouteGeometryError,
    RoutePosition,
    build_route_line,
    decimate_by_stride,
    estimated_detour_miles,
    haversine_miles,
    is_within_detour_limit,
    miles_from_meters,
    project_onto_segment,
    quantise_miles,
    simplify_for_response,
    simplify_polyline,
)

from .base import OfflineTestCase

SEED = 20261027
METERS_PER_DEGREE_LATITUDE = 111195.08  # R = 6371.0088 km
R600_DISTANCE_M = 965606.4  # exactly 600.0 miles


def degrees_for_miles(miles: float) -> float:
    """Degrees of latitude that span `miles` along a meridian."""
    return math.degrees(miles / geo.EARTH_RADIUS_MILES)


def degrees_for_meters(meters: float) -> float:
    """Degrees of latitude that span `meters` along a meridian."""
    return meters / METERS_PER_DEGREE_LATITUDE


def r600_points() -> list[LonLat]:
    """The 600-mile test route: lat 40.0, lon -110 to -98 every 0.5° (25 points, 24 × 25 mi)."""
    return [(-110.0 + 0.5 * step, 40.0) for step in range(25)]


def r600_corridor() -> RouteCorridor:
    line = build_route_line(r600_points(), length_miles=miles_from_meters(R600_DISTANCE_M))
    return RouteCorridor(line)


def line_length_miles(points: list[LonLat]) -> float:
    """Raw haversine length, used where a test needs an L that matches its geometry."""
    return sum(
        haversine_miles(lat1=lat1, lon1=lon1, lat2=lat2, lon2=lon2)
        for (lon1, lat1), (lon2, lat2) in zip(points, points[1:], strict=False)
    )


def move(point: LonLat, *, miles: float, heading_rad: float) -> LonLat:
    """A point `miles` away on a local flat map (heading 0 = north)."""
    lon, lat = point
    delta_lat = miles * math.cos(heading_rad) / MILES_PER_DEGREE_LATITUDE
    delta_lon = (
        miles * math.sin(heading_rad) / (MILES_PER_DEGREE_LATITUDE * math.cos(math.radians(lat)))
    )
    return (lon + delta_lon, lat + delta_lat)


def random_walk_route(rng: random.Random, *, vertex_count: int) -> list[LonLat]:
    """A wandering road inside the lower 48, with duplicates (step 0) and some U-turns."""
    point = (rng.uniform(-124.0, -67.0), rng.uniform(25.0, 49.0))
    heading = rng.uniform(0.0, 2 * math.pi)
    points = [point]
    for _ in range(vertex_count - 1):
        heading += rng.gauss(0.0, 0.6) + (math.pi if rng.random() < 0.03 else 0.0)
        point = move(point, miles=rng.choice((0.0, 0.3, 1.0, 3.0, 5.0)), heading_rad=heading)
        points.append(point)
    return points


def synthetic_new_york_to_los_angeles(rng: random.Random) -> list[LonLat]:
    """Stand-in for the OSRM NY→LA geometry (34,664 points, FACTS) until 2C records it.

    A straight line with a 5 km sinusoid (50 km wavelength) and ±20 m noise; ends exact.
    """
    count = 34_664
    new_york, los_angeles = (-74.01, 40.71), (-118.24, 34.05)
    total_km = haversine_miles(lat1=40.71, lon1=-74.01, lat2=34.05, lon2=-118.24) * 1.609344
    points = [new_york]
    for index in range(1, count - 1):
        fraction = index / (count - 1)
        lon = new_york[0] + fraction * (los_angeles[0] - new_york[0])
        lat = new_york[1] + fraction * (los_angeles[1] - new_york[1])
        wiggle_m = 5000.0 * math.sin(2 * math.pi * fraction * total_km / 50.0)
        lat += degrees_for_meters(wiggle_m + rng.uniform(-20.0, 20.0))
        lon += degrees_for_meters(rng.uniform(-20.0, 20.0)) / math.cos(math.radians(lat))
        points.append((lon, lat))
    points.append(los_angeles)
    return points


class HaversineTests(OfflineTestCase):
    """Supports TC-14: the distance every marker and offset is built from."""

    def test_one_degree_of_latitude_is_r_times_pi_over_180(self) -> None:
        miles = haversine_miles(lat1=40.0, lon1=-100.0, lat2=41.0, lon2=-100.0)

        self.assertAlmostEqual(miles, 69.093418985531, places=9)
        self.assertAlmostEqual(MILES_PER_DEGREE_LATITUDE, 69.093418985531, places=9)

    def test_one_degree_of_longitude_on_the_equator_equals_one_degree_of_latitude(self) -> None:
        miles = haversine_miles(lat1=0.0, lon1=0.0, lat2=0.0, lon2=1.0)

        self.assertAlmostEqual(miles, 69.093418985531, places=9)

    def test_quarter_meridian_and_antipode(self) -> None:
        quarter = haversine_miles(lat1=90.0, lon1=0.0, lat2=0.0, lon2=0.0)
        antipode = haversine_miles(lat1=0.0, lon1=0.0, lat2=0.0, lon2=180.0)

        self.assertAlmostEqual(quarter, 6218.407708697789, places=9)
        self.assertAlmostEqual(antipode, math.pi * geo.EARTH_RADIUS_MILES, places=9)

    def test_same_point_is_exactly_zero(self) -> None:
        self.assertEqual(haversine_miles(lat1=40.0, lon1=-100.0, lat2=40.0, lon2=-100.0), 0.0)

    def test_distance_is_symmetric(self) -> None:
        there = haversine_miles(lat1=40.6398, lon1=-73.7789, lat2=33.9425, lon2=-118.4081)
        back = haversine_miles(lat1=33.9425, lon1=-118.4081, lat2=40.6398, lon2=-73.7789)

        self.assertAlmostEqual(there, 2469.46016397, places=7)
        self.assertEqual(there, back)

    def test_one_degree_of_longitude_at_40n_is_shorter_than_the_parallel_arc(self) -> None:
        miles = haversine_miles(lat1=40.0, lon1=-100.0, lat2=40.0, lon2=-99.0)

        self.assertAlmostEqual(miles, 52.92835209759775, places=9)
        self.assertLess(miles, MILES_PER_DEGREE_LATITUDE * math.cos(math.radians(40.0)))

    def test_meters_convert_to_miles(self) -> None:
        self.assertEqual(miles_from_meters(R600_DISTANCE_M), 600.0)


class CumulativeMilesTests(OfflineTestCase):
    """TC-14 precondition (§15): markers scaled so the finish equals the OSRM distance."""

    def test_markers_on_an_even_parallel_scale_to_the_osrm_distance(self) -> None:
        line = build_route_line(r600_points(), length_miles=miles_from_meters(R600_DISTANCE_M))

        for step, mile in enumerate(line.cumulative_miles):
            self.assertAlmostEqual(mile, 25.0 * step, places=9)
        self.assertEqual(line.cumulative_miles[0], 0.0)
        self.assertEqual(line.cumulative_miles[-1], 600.0)

    def test_duplicate_vertex_gives_equal_consecutive_markers(self) -> None:
        a, b, c = (-100.0, 40.0), (-99.0, 40.0), (-98.0, 40.0)

        markers = build_route_line([a, a, b, c], length_miles=100.0).cumulative_miles

        self.assertEqual(markers[0], 0.0)
        self.assertEqual(markers[1], 0.0)
        self.assertLess(markers[1], markers[2])
        self.assertLess(markers[2], markers[3])

    def test_line_ending_in_a_repeated_point_never_marks_past_the_finish(self) -> None:
        # Unclamped, raw * (L / raw) lands 1 ulp above L for this a, b and L.
        a, b = (-100.0, 40.0), (-99.999, 40.0)
        length_miles = 449.31206115537026

        markers = build_route_line([a, b, b], length_miles=length_miles).cumulative_miles

        self.assertLessEqual(markers[1], length_miles, f"marker {markers[1]!r} past L")
        self.assertTrue(all(x <= y for x, y in zip(markers, markers[1:], strict=False)))

    def test_route_of_identical_points_does_not_divide_by_zero(self) -> None:
        point = (-100.0, 40.0)

        zero_length = build_route_line([point, point], length_miles=0.0)
        osrm_says_longer = build_route_line([point, point, point], length_miles=3.0)

        self.assertEqual(zero_length.cumulative_miles, (0.0, 0.0))
        self.assertEqual(osrm_says_longer.cumulative_miles, (0.0, 0.0, 3.0))

    def test_markers_are_non_decreasing_on_random_walks(self) -> None:
        rng = random.Random(SEED)
        for walk in range(50):
            points = random_walk_route(rng, vertex_count=rng.randint(2, 200))
            length_miles = line_length_miles(points) * rng.uniform(1.0, 1.1)

            markers = build_route_line(points, length_miles=length_miles).cumulative_miles

            with self.subTest(walk=walk, seed=SEED):
                self.assertEqual(len(markers), len(points))
                self.assertEqual(markers[0], 0.0)
                self.assertEqual(markers[-1], length_miles)
                self.assertTrue(all(a <= b for a, b in zip(markers, markers[1:], strict=False)))

    def test_fewer_than_two_points_or_a_bad_length_is_a_geometry_error(self) -> None:
        cases = {
            "one point": ([(-100.0, 40.0)], 1.0),
            "nan coordinate": ([(-100.0, 40.0), (math.nan, 40.0)], 1.0),
            "negative length": ([(-100.0, 40.0), (-99.0, 40.0)], -1.0),
            "infinite length": ([(-100.0, 40.0), (-99.0, 40.0)], math.inf),
        }
        for name, (points, length_miles) in cases.items():
            with self.subTest(name), self.assertRaises(RouteGeometryError):
                build_route_line(points, length_miles=length_miles)


class QuantiseAndThresholdTests(OfflineTestCase):
    """TC-14 raw floats (§8 #10, §15): quantise once from repr, HALF_UP; d = 2q <= 5.00."""

    def test_offsets_quantise_from_repr_half_up(self) -> None:
        # raw offset -> (quantised offset, detour, kept)
        cases = {
            2.4999999999: ("2.50", "5.00", True),
            2.504999: ("2.50", "5.00", True),
            2.505: ("2.51", "5.02", False),  # Decimal(2.505) would give 2.50
            2.51: ("2.51", "5.02", False),
            0.0: ("0.00", "0.00", True),
        }
        for raw, (offset, detour, is_kept) in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(quantise_miles(raw), Decimal(offset))
                self.assertEqual(estimated_detour_miles(raw), Decimal(detour))
                self.assertIs(is_within_detour_limit(estimated_detour_miles(raw)), is_kept)

    def test_miles_quantise_half_up_to_hundredths(self) -> None:
        cases = {
            0.0049999: "0.00",
            0.005: "0.01",
            1e-05: "0.00",
            412.645: "412.65",  # Decimal(412.645) would give 412.64
            2799.995: "2800.00",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(str(quantise_miles(raw)), expected)

    def test_quantised_value_is_a_decimal_and_never_negative_zero(self) -> None:
        result = quantise_miles(-0.0)

        self.assertIs(type(result), Decimal)
        self.assertEqual(str(result), "0.00")
        self.assertEqual(str(quantise_miles(-0.004)), "0.00")

    def test_detour_is_twice_the_quantised_offset(self) -> None:
        self.assertEqual(estimated_detour_miles(2.506), 2 * Decimal("2.51"))

    def test_non_finite_distance_is_a_geometry_error(self) -> None:
        for raw in (math.nan, math.inf, -math.inf):
            with self.subTest(raw=raw), self.assertRaises(RouteGeometryError):
                quantise_miles(raw)

    def test_corridor_pad_covers_the_detour_limit_half_plus_rounding(self) -> None:
        self.assertGreater(Decimal(repr(CORRIDOR_PAD_MILES)), MAX_DETOUR_MILES / 2 + MILE_STEP / 2)


class CorridorAndDetourTests(OfflineTestCase):
    """TC-14 (`test_corridor_and_detour`): stations projected onto the 600-mile route R600."""

    def assertPosition(self, position: RoutePosition | None, *, mile: str, offset: str) -> None:
        self.assertIsNotNone(position)
        assert position is not None  # narrows the type for the checks below
        self.assertEqual(position.route_mile, Decimal(mile))
        self.assertEqual(position.offset_miles, Decimal(offset))
        self.assertEqual(position.estimated_detour_miles, 2 * Decimal(offset))

    def test_station_exactly_on_the_corridor_edge_is_kept(self) -> None:
        position = r600_corridor().locate(
            latitude=40.0 + degrees_for_miles(2.50), longitude=-104.25
        )

        self.assertPosition(position, mile="287.50", offset="2.50")

    def test_station_just_outside_the_corridor_is_dropped(self) -> None:
        # South of lat 40.0: a different cell row from the route, so padding is exercised too.
        position = r600_corridor().locate(latitude=40.0 - degrees_for_miles(2.51), longitude=-101.0)

        self.assertIsNone(position)

    def test_station_on_the_route_has_zero_detour(self) -> None:
        position = r600_corridor().locate(latitude=40.0, longitude=-104.0)

        self.assertPosition(position, mile="300.00", offset="0.00")

    def test_station_projects_onto_the_nearest_segment_at_the_perpendicular_foot(self) -> None:
        position = r600_corridor().locate(latitude=40.0 + degrees_for_miles(1.0), longitude=-104.25)

        self.assertPosition(position, mile="287.50", offset="1.00")

    def test_station_behind_the_start_clamps_to_mile_zero(self) -> None:
        position = r600_corridor().locate(latitude=40.0, longitude=-110.0 - 0.01889336652461281)

        self.assertPosition(position, mile="0.00", offset="1.00")

    def test_station_past_the_finish_clamps_to_route_length(self) -> None:
        position = r600_corridor().locate(latitude=40.0, longitude=-98.0 + 0.0188934)

        self.assertPosition(position, mile="600.00", offset="1.00")

    def test_station_far_from_any_segment_has_no_position(self) -> None:
        self.assertIsNone(r600_corridor().locate(latitude=45.0, longitude=-104.0))

    def test_station_outside_a_convex_corner_projects_to_the_corner_vertex(self) -> None:
        points = [(-100.0, 40.0), (-99.0, 40.0), (-99.0, 41.0)]
        line = build_route_line(points, length_miles=line_length_miles(points))

        position = RouteCorridor(line).locate(
            latitude=40.0 - degrees_for_miles(1.0), longitude=-99.0 + 0.0188934
        )

        self.assertPosition(
            position, mile=str(quantise_miles(line.cumulative_miles[1])), offset="1.41"
        )

    def test_zero_length_segment_does_not_crash_projection(self) -> None:
        points = r600_points()
        points.insert(13, points[12])  # (-104.0, 40.0) twice
        line = build_route_line(points, length_miles=600.0)

        position = RouteCorridor(line).locate(
            latitude=40.0 + degrees_for_miles(1.0), longitude=-104.0
        )

        self.assertPosition(position, mile="300.00", offset="1.00")

    def test_out_and_back_tie_takes_the_smaller_mile(self) -> None:
        a, b = (-100.0, 40.0), (-99.9, 40.0)
        line = build_route_line([a, b, a], length_miles=2 * line_length_miles([a, b]))

        position = RouteCorridor(line).locate(latitude=40.01, longitude=-99.95)

        assert position is not None
        self.assertEqual(position.route_mile, quantise_miles(line.length_miles / 4))

    def test_grid_pads_longitude_by_cos_latitude_near_the_northern_border(self) -> None:
        # Padding by 2.6 mi of *latitude* degrees would stop one cell short of this station.
        points = [(-100.04, 48.4), (-100.04, 48.6)]
        line = build_route_line(points, length_miles=line_length_miles(points))

        position = RouteCorridor(line).locate(latitude=48.5, longitude=-99.98648631856439)

        self.assertIsNotNone(position)
        assert position is not None
        self.assertEqual(position.offset_miles, Decimal("2.45"))
        self.assertEqual(position.estimated_detour_miles, Decimal("4.90"))

    def test_grid_pads_longitude_for_the_northern_end_of_a_long_segment(self) -> None:
        # Padding with cos(min |lat|) (45° here) would leave this station's cell out.
        points = [(-99.9535, 45.0), (-99.9535, 48.95)]
        line = build_route_line(points, length_miles=line_length_miles(points))

        position = RouteCorridor(line).locate(latitude=48.9, longitude=-99.89955936389673)

        self.assertPosition(position, mile="269.46", offset="2.45")

    def test_station_beside_a_diagonal_segment_uses_east_west_distances_scaled_by_latitude(
        self,
    ) -> None:
        # Station 2.45 mi perpendicular (south-east) of the midpoint of a north-east segment.
        # Projecting without cos(latitude) would put the foot elsewhere: 2.58 mi, dropped.
        points = [(-100.0, 45.0), (-100.0 + 0.5 / math.cos(math.radians(45.0)), 45.5)]
        line = build_route_line(points, length_miles=line_length_miles(points))

        position = RouteCorridor(line).locate(
            latitude=45.22498153340568, longitude=-99.61075367901516
        )

        self.assertPosition(position, mile="24.38", offset="2.45")


class SegmentGridMatchesBruteForceTests(OfflineTestCase):
    """TC-14: the one-cell grid lookup gives the same answer as scanning every segment."""

    def test_grid_lookup_equals_exhaustive_scan_on_random_routes(self) -> None:
        rng = random.Random(SEED)
        candidates = near_edge = 0
        for route in range(40):
            points = random_walk_route(rng, vertex_count=rng.randint(30, 150))
            line = build_route_line(points, length_miles=line_length_miles(points))
            corridor = RouteCorridor(line)
            for _ in range(250):
                lon, lat = move(
                    rng.choice(points),
                    miles=rng.uniform(0.0, 4.0),
                    heading_rad=rng.uniform(0.0, 2 * math.pi),
                )
                expected, raw_offset = self._scan_every_segment(line, latitude=lat, longitude=lon)

                actual = corridor.locate(latitude=lat, longitude=lon)

                self.assertEqual(actual, expected, f"route {route}, station ({lat}, {lon})")
                candidates += actual is not None
                near_edge += 2.4 < raw_offset < 2.6
        self.assertGreaterEqual(candidates, 5_000, f"seed {SEED}: {candidates} candidates")
        self.assertGreaterEqual(near_edge, 100, f"seed {SEED}: {near_edge} near the 2.5 mi edge")

    @staticmethod
    def _scan_every_segment(
        line: geo.RouteLine, *, latitude: float, longitude: float
    ) -> tuple[RoutePosition | None, float]:
        """Reference: min over all segments of (quantised offset, mile), then the 5.00 rule."""
        best: tuple[Decimal, float, float] | None = None
        miles = line.cumulative_miles
        for segment in range(len(line.points) - 1):
            foot = project_onto_segment(
                latitude=latitude,
                longitude=longitude,
                start=line.points[segment],
                end=line.points[segment + 1],
            )
            mile = miles[segment] + foot.fraction * (miles[segment + 1] - miles[segment])
            mile = min(max(mile, 0.0), line.length_miles)
            key = (quantise_miles(foot.offset_miles), mile, foot.offset_miles)
            if best is None or key[:2] < best[:2]:
                best = key
        assert best is not None  # every route has at least one segment
        offset, mile, raw_offset = best
        if not is_within_detour_limit(2 * offset):
            return None, raw_offset
        position = RoutePosition(
            route_mile=quantise_miles(mile), offset_miles=offset, estimated_detour_miles=2 * offset
        )
        return position, raw_offset


class SimplifyTests(OfflineTestCase):
    """TC-22 (NFR-6): Douglas–Peucker keeps the shape within tolerance and the exact ends."""

    def assertEndsKept(self, simplified: tuple[LonLat, ...], original: list[LonLat]) -> None:
        self.assertEqual(simplified[0], original[0])
        self.assertEqual(simplified[-1], original[-1])

    def test_collinear_points_reduce_to_the_two_endpoints(self) -> None:
        points = [(-100.0 + step / 100, 40.0) for step in range(101)]

        for tolerance_m in (10, 50):
            with self.subTest(tolerance_m=tolerance_m):
                simplified = simplify_polyline(points, tolerance_miles=tolerance_m / 1609.344)
                self.assertEqual(simplified, (points[0], points[-1]))

    def test_apex_beyond_tolerance_is_kept_and_within_is_dropped(self) -> None:
        # (tolerance m, apex m) -> expected point count
        cases = {(50, 60.0): 3, (50, 40.0): 2, (10, 12.0): 3, (10, 8.0): 2}
        for (tolerance_m, apex_m), expected_count in cases.items():
            points = [(-100.0, 40.0), (-99.5, 40.0 + degrees_for_meters(apex_m)), (-99.0, 40.0)]

            simplified = simplify_polyline(points, tolerance_miles=tolerance_m / 1609.344)

            with self.subTest(tolerance_m=tolerance_m, apex_m=apex_m):
                self.assertEqual(len(simplified), expected_count, f"{len(simplified)} positions")
                self.assertEndsKept(simplified, points)

    def test_apex_offset_east_of_a_north_south_chord_is_measured_in_miles(self) -> None:
        # Without cos(latitude) on longitude, 40 m east at lat 40.5 would look like 53 m.
        metres_per_degree_longitude = METERS_PER_DEGREE_LATITUDE * math.cos(math.radians(40.5))
        cases = {60.0: 3, 40.0: 2}  # apex m east -> expected point count at DP 50 m
        for apex_m, expected_count in cases.items():
            points = [
                (-100.0, 40.0),
                (-100.0 + apex_m / metres_per_degree_longitude, 40.5),
                (-100.0, 41.0),
            ]

            simplified = simplify_polyline(points, tolerance_miles=RESPONSE_TOLERANCE_MILES)

            with self.subTest(apex_m=apex_m):
                self.assertEqual(len(simplified), expected_count, f"{len(simplified)} positions")

    def test_u_turn_tip_beyond_the_chord_end_is_kept(self) -> None:
        # A spur: out 1 mi east, back to 0.5 mi. The tip B lies on the chord's infinite line,
        # so only the distance to the chord *segment* keeps it.
        a = (-100.0, 40.0)
        b = (-100.0 + 1.0 / (MILES_PER_DEGREE_LATITUDE * math.cos(math.radians(40.0))), 40.0)
        c = ((a[0] + b[0]) / 2, 40.0)

        simplified = simplify_polyline([a, b, c], tolerance_miles=ROUTE_TOLERANCE_MILES)

        self.assertEqual(simplified, (a, b, c))

    def test_response_line_is_simplified_before_the_cap(self) -> None:
        points = [(-100.0 + step / 100, 40.0) for step in range(101)]

        self.assertEqual(simplify_for_response(points), (points[0], points[-1]))

    def test_negative_or_nan_tolerance_is_a_geometry_error(self) -> None:
        points = [(-100.0, 40.0), (-99.0, 40.0)]
        for tolerance_miles in (-0.01, math.nan):
            with (
                self.subTest(tolerance_miles=tolerance_miles),
                self.assertRaises(RouteGeometryError),
            ):
                simplify_polyline(points, tolerance_miles=tolerance_miles)

    def test_loop_with_coincident_endpoints_keeps_the_far_point(self) -> None:
        p, q = (-100.0, 40.0), (-100.0, 40.0 + degrees_for_miles(1.0))

        simplified = simplify_polyline([p, q, p], tolerance_miles=ROUTE_TOLERANCE_MILES)

        self.assertEqual(simplified, (p, q, p))

    def test_duplicate_points_do_not_crash_simplify(self) -> None:
        p, q = (-100.0, 40.0), (-99.0, 40.0)

        self.assertEqual(
            simplify_polyline([p, p, q], tolerance_miles=ROUTE_TOLERANCE_MILES), (p, q)
        )
        self.assertEqual(
            simplify_polyline([p, p, p], tolerance_miles=ROUTE_TOLERANCE_MILES), (p, p)
        )

    def test_two_point_route_is_unchanged(self) -> None:
        points = [(-100.0, 40.0), (-99.0, 41.0)]

        self.assertEqual(simplify_for_response(points), tuple(points))

    def test_fewer_than_two_points_is_a_geometry_error(self) -> None:
        with self.assertRaises(RouteGeometryError):
            simplify_polyline([(-100.0, 40.0)], tolerance_miles=ROUTE_TOLERANCE_MILES)

    def test_zigzag_route_falls_back_to_stride_below_2000_positions(self) -> None:
        # Random amplitudes (89-178 m, all over 50 m): Douglas-Peucker keeps every apex.
        # A constant amplitude would make Douglas-Peucker O(n²); see the 1C design.
        rng = random.Random(SEED)
        points = [
            (-100.0 + 0.002 * index, 40.0 + (rng.uniform(0.0008, 0.0016) if index % 2 else 0.0))
            for index in range(6_000)
        ]

        after_douglas_peucker = simplify_polyline(points, tolerance_miles=50 / 1609.344)
        response = simplify_for_response(points)

        self.assertEqual(len(after_douglas_peucker), 6_000, "every apex is beyond 50 m")
        self.assertLessEqual(len(response), RESPONSE_MAX_POSITIONS, f"{len(response)} positions")
        self.assertEndsKept(response, points)
        index_of = {point: index for index, point in enumerate(points)}
        indices = [index_of[point] for point in response]
        self.assertEqual(indices, sorted(set(indices)), "a subsequence in route order")

    def test_new_york_to_los_angeles_is_under_2000_positions_with_endpoints(self) -> None:
        # Synthetic stand-in (owner decision 2026-10-27); 2C reruns this on the OSRM recording.
        points = synthetic_new_york_to_los_angeles(random.Random(SEED))

        cached_10m = simplify_polyline(points, tolerance_miles=ROUTE_TOLERANCE_MILES)
        response = simplify_for_response(cached_10m)

        self.assertEqual(len(points), 34_664)
        self.assertLess(len(response), 2_000, f"{len(cached_10m)} -> {len(response)} positions")
        self.assertEndsKept(cached_10m, points)
        self.assertEndsKept(response, points)


class PositionCapTests(OfflineTestCase):
    """TC-22 (NFR-6): the stride fallback keeps fewer than 2,000 positions and both ends."""

    def test_position_cap_boundaries(self) -> None:
        # 3,998 and 5,996 are the off-by-one traps of a step of ceil(n / cap).
        for count in (2, 1_999, 2_000, 2_001, 3_997, 3_998, 5_996, 34_664, 100_000):
            points = [(-120.0 + index * 1e-4, 40.0) for index in range(count)]

            capped = decimate_by_stride(points, max_positions=RESPONSE_MAX_POSITIONS)

            with self.subTest(count=count):
                if count <= RESPONSE_MAX_POSITIONS:
                    self.assertEqual(capped, tuple(points))
                self.assertLessEqual(
                    len(capped), RESPONSE_MAX_POSITIONS, f"{len(capped)} positions"
                )
                self.assertEqual(capped[0], points[0])
                self.assertEqual(capped[-1], points[-1])
                self.assertEqual(len(set(capped)), len(capped), "no point repeated")
                self.assertEqual(list(capped), sorted(capped), "route order kept")

    def test_cap_below_two_positions_is_a_geometry_error(self) -> None:
        with self.assertRaises(RouteGeometryError):
            decimate_by_stride([(-100.0, 40.0), (-99.0, 40.0), (-98.0, 40.0)], max_positions=1)


class GeoPurityTests(OfflineTestCase):
    """TC-41, geo half (NFR-13): geo.py imports only pure standard-library modules."""

    ALLOWED_MODULES = {"math", "decimal", "dataclasses", "collections.abc", "typing"}

    def test_geo_imports_only_pure_standard_library_modules(self) -> None:
        tree = ast.parse(Path(geo.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add("." * node.level + (node.module or ""))

        self.assertLessEqual(imported, self.ALLOWED_MODULES, f"geo.py imports {sorted(imported)}")
