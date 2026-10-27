"""Spec 02: the map page of a saved plan (TC-1, 2, 4, 6, 7, 9–19).

Most tests seed a planobj straight into the cache (the §7a golden body without `meta`
and `map_url`) and read the page with a CSRF-enforcing client. Routing and planning are
patched to fail, so any routing call on a map path turns a test red (NFR-1). TC-2 and
the start = finish case run the real planner first, as spec 01's TC-45 does.
"""

import ast
import copy
import inspect
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import cache, caches
from django.db import DatabaseError, connection
from django.http import HttpResponse
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from fuelplan import map_page, views
from fuelplan.planner import planobj_cache_key
from fuelplan.routing import RoutingClient

from .base import ApiTestCase, assert_envelope, cache_keys, expire_cache_key, ttl_of
from .planner_harness import PlannerHarness, json_body
from .test_response_body import GOLDEN_BODY, GOLDEN_REQUEST, golden_index

PLAN_ID = GOLDEN_BODY["plan_id"]
OTHER_ID = "3b9f4c1e-8a2d-4f6b-9e1c-7d5a0b2c4e6f"
TEMPLATE = Path(views.__file__).resolve().parent / "templates" / "fuelplan" / "map.html"
HOSTILE = "</script><img src=x onerror=alert(1)>"
LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
LEAFLET_CSS_SRI = "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
LEAFLET_JS_SRI = "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_COPYRIGHT = "https://www.openstreetmap.org/copyright"
GEOAPIFY = "https://www.geoapify.com/"

# §7b for the golden plan, with the 2026-10-28 amendments (burns, purchase rule, range).
GOLDEN_HEADER = [
    "Chicago, IL → Denver, CO",
    "Total fuel cost $183.04 · 56 gal bought · 2 fuel stops",
    "(each stop cost is rounded to the cent; the total is rounded once)",
    "Route 1003.2 mi · driven 1005.2 mi incl. detours · finish with 5.4 gal · burns 100.5 gal",
    "Start tank (50.0 gal) is not charged · reserve 5.0 gal. Prices: provided CSV snapshot, "
    "not live.",
]
GOLDEN_ROWS = [
    "S · Chicago, IL · start with 50.0 gal",
    "1 · EXAMPLE TRAVEL CENTER #1 (OPIS 4021) · I-80, EXIT 46, Walnut, IA",
    "Mile 412.6 · arrive with 8.6 gal · buy 41 gal × $3.199 (CSV price) = $131.16 · fill tank",
    "Next leg 433.0 mi · ~1.2 mi extra driving (off and back) (estimated)",
    "Location: poi_match",
    "2 · EXAMPLE TRAVEL CENTER #2 (OPIS 7310) · I-76, EXIT 125, Sterling, CO",
    "Mile 844.6 · arrive with 6.3 gal · buy 15 gal × $3.459 (CSV price) = $51.89 · just enough",
    "Next leg 159.0 mi · ~0.8 mi extra driving (off and back) (estimated)",
    "Location: address_match",
    "F · Denver, CO · arrive with 5.4 gal",
]
LEGEND = (
    "Location: poi_match = matched to the station itself · address_match = matched to its "
    "street address · approximate = only the town or road was found."
)
RANGE_LINE = "Range: first leg 450.0 mi · after a purchase 440.0 mi · hard max 450.0 mi"
DETOURS_NOTICE = (
    "Detours are straight-line estimates, not measured on roads. Each stop's estimated extra "
    "driving is at most 5.0 mi; the real drive off the highway can be longer. The plan keeps "
    "1.0 mi of extra fuel at each end of every leg that has a stop (4.0 mi in total). Stops "
    "marked (estimated) are not road-checked."
)
ZERO_STOP = "No fuel stop needed. Total fuel cost $0.00 — the start tank covers this trip."
TILES_FAILED = (
    "Map tiles could not be loaded (offline or the tile server refused the request). "
    "The route details, stops and totals on this page are still correct."
)
NOSCRIPT = "The map needs JavaScript; all plan details are listed on this page."
# Spec 01 wording (serializers), as stored with a plan.
APPROXIMATE_WARNING = (
    "Station 7310 (EXAMPLE TRAVEL CENTER #2, Sterling CO) has only a town-level location; "
    "its real position and detour may differ."
)
FAR_SNAP_WARNING = (
    "The start point is 1.2 mi from the nearest road; the route starts there, and those "
    "miles are not counted."
)
RULE_WORDS = {"fill_tank": "fill tank", "to_next_stop": "just enough", "to_finish": "just enough"}
BLOCK_TAGS = frozenset(
    {"h1", "h2", "p", "li", "ul", "ol", "div", "header", "main", "footer", "noscript", "body"}
)
HIDDEN_TEXT_TAGS = frozenset({"script", "style", "title"})


def planobj() -> dict:
    """The saved-plan entry of the golden plan: its POST body without `meta` and `map_url`."""
    body = copy.deepcopy(GOLDEN_BODY)
    body.pop("meta")
    body.pop("map_url")
    return body


def seed(body: object, *, plan_id: str = PLAN_ID) -> None:
    """Save a plan as the planner does (spec 01 §6 key and TTL)."""
    cache.set(planobj_cache_key(plan_id), body, settings.FUELPILOT_PLAN_TTL_S)


def map_path(plan_id: str = PLAN_ID) -> str:
    return f"/api/v1/fuel-plans/{plan_id}/map"


@dataclass
class Script:
    attrs: dict[str, str | None]
    text: str = ""


@dataclass
class ParsedPage:
    """A page read the way a person (or a no-JS browser) reads it."""

    lines: list[str] = field(default_factory=list)  # visible text, one entry per block
    elements: list[tuple[str, dict[str, str | None]]] = field(default_factory=list)
    scripts: list[Script] = field(default_factory=list)
    noscript: str = ""
    map_index: int = -1  # number of text lines before <div id="map">

    @property
    def plan_data(self) -> dict:
        (data,) = [s for s in self.scripts if s.attrs.get("id") == "plan-data"]
        return json.loads(data.text)

    @property
    def inline_script(self) -> str:
        (script,) = [s for s in self.scripts if "src" not in s.attrs and "type" not in s.attrs]
        return script.text

    def count(self, line: str) -> int:
        return self.lines.count(line)

    def index(self, line: str) -> int:
        return self.lines.index(line)

    def row_labels(self) -> list[str]:
        return [point["label"] for point in self.plan_data["points"]]


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.page = ParsedPage()
        self._buffer: list[str] = []
        self._hidden_depth = 0  # inside <script>, <style> or <title>: not visible text
        self._script: Script | None = None
        self._in_noscript = False

    def _flush(self) -> None:
        text = " ".join("".join(self._buffer).split())
        if text:
            self.page.lines.append(text)
        self._buffer = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.page.elements.append((tag, attributes))
        if tag in BLOCK_TAGS:
            self._flush()
        if tag == "div" and attributes.get("id") == "map":
            self.page.map_index = len(self.page.lines)
        if tag == "script":
            self._script = Script(attrs=attributes)
            self.page.scripts.append(self._script)
        if tag == "noscript":
            self._in_noscript = True
        if tag in HIDDEN_TEXT_TAGS:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in HIDDEN_TEXT_TAGS:
            self._hidden_depth -= 1
        if tag == "script":
            self._script = None
        if tag == "noscript":
            self._in_noscript = False
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.text += data
        elif not self._hidden_depth:
            self._buffer.append(data)
            if self._in_noscript:
                self.page.noscript += data


def parse_page(content: bytes) -> ParsedPage:
    parser = _PageParser()
    parser.feed(content.decode("utf-8"))
    parser.close()
    return parser.page


class SeededMapTestCase(ApiTestCase):
    """A CSRF-enforcing client and a map view that must never route or plan."""

    def setUp(self) -> None:
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)
        for target in (
            mock.patch.object(
                RoutingClient, "fetch_route", side_effect=AssertionError("the map page routed")
            ),
            mock.patch(
                "fuelplan.views.build_trip_planner",
                side_effect=AssertionError("the map page planned"),
            ),
        ):
            target.start()
            self.addCleanup(target.stop)

    def page_of(self, body: dict) -> ParsedPage:
        """Seed `body` as the saved plan and read its page (200 expected)."""
        seed(body)
        response = self.client.get(map_path())
        self.assertEqual(response.status_code, 200, response.content)
        return parse_page(response.content)

    def assert_read_only(
        self, send: Callable[[], HttpResponse], *, plan_id: str = PLAN_ID, queries: int
    ) -> HttpResponse:
        """`queries` SQL statements, no INSERT/UPDATE, and a plan still saved keeps its TTL.

        The only DELETE allowed is Django's own, when it reads an expired row (NFR-2).
        """
        key = planobj_cache_key(plan_id)
        ttl_before = ttl_of(key) if key in cache_keys() else None
        with CaptureQueriesContext(connection) as captured:
            response = send()
        statements = [query["sql"].lstrip().upper() for query in captured.captured_queries]
        self.assertEqual(len(statements), queries, statements)
        self.assertFalse([sql for sql in statements if sql.startswith(("INSERT", "UPDATE"))])
        is_expired = ttl_before is not None and ttl_before < 0
        deletes = [sql for sql in statements if sql.startswith("DELETE")]
        self.assertEqual(len(deletes), 1 if is_expired else 0)
        if ttl_before is not None and not is_expired:
            self.assertAlmostEqual(ttl_of(key), ttl_before, delta=1.0)
        return response


class SavedPlanPageTests(SeededMapTestCase):
    """TC-1 (FR-1, FR-14, NFR-1, NFR-2, NFR-6–8, Q-2)."""

    def test_a_saved_plan_is_an_html_page_with_the_tile_referrer_policy_and_no_cookie(
        self,
    ) -> None:
        seed(planobj())

        response = self.assert_read_only(lambda: self.client.get(map_path()), queries=1)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(response["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response.cookies, {})
        self.assertNotIn("Set-Cookie", response)

    def test_the_route_name_still_reverses_to_the_spec_01_path(self) -> None:
        path = reverse("fuelplan:fuel-plan-map", kwargs={"plan_id": PLAN_ID})

        self.assertEqual(path, f"/api/v1/fuel-plans/{PLAN_ID}/map")

    def test_repeat_gets_and_a_query_string_give_the_same_bytes_and_head_the_same_headers(
        self,
    ) -> None:
        seed(planobj())

        first, second = self.client.get(map_path()), self.client.get(map_path())
        with_query = self.client.get(map_path() + "?x=1")
        head = self.client.head(map_path())

        self.assertEqual(first.content, second.content)
        self.assertEqual(first.content, with_query.content)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        for header in ("Content-Type", "Referrer-Policy", "X-Frame-Options"):
            self.assertEqual(head[header], first[header], header)

    def test_viewing_a_saved_plan_writes_nothing_and_keeps_its_ttl(self) -> None:
        seed(planobj())

        response = self.assert_read_only(lambda: self.client.get(map_path()), queries=1)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(cache.get(planobj_cache_key(PLAN_ID)), planobj())

    def test_the_map_view_never_names_the_planner_or_routing(self) -> None:
        tree = ast.parse(inspect.getsource(views.fuel_plan_map))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

        forbidden = {"build_trip_planner", "TripPlanner", "CallBudget", "CALL_LIMIT"}
        forbidden |= {"RoutingClient", "fetch_route"}
        self.assertEqual(names & forbidden, set())
        self.assertIn("planobj_cache_key", names)

    def test_the_page_builder_imports_only_the_standard_library(self) -> None:
        tree = ast.parse(Path(map_page.__file__).read_text(encoding="utf-8"))
        imports = [node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)]
        modules = [
            alias.name for node in imports if isinstance(node, ast.Import) for alias in node.names
        ]
        modules += [node.module or "" for node in imports if isinstance(node, ast.ImportFrom)]

        self.assertTrue(
            all(node.level == 0 for node in imports if isinstance(node, ast.ImportFrom))
        )
        self.assertTrue(all(name.split(".")[0] in sys.stdlib_module_names for name in modules))

    def test_the_view_has_at_most_30_lines_and_the_inline_script_at_most_50(self) -> None:
        page = self.page_of(planobj())

        self.assertLessEqual(len(inspect.getsource(views.fuel_plan_map).splitlines()), 30)
        self.assertLessEqual(len(page.inline_script.strip().splitlines()), 50)

    @override_settings(GEOAPIFY_API_KEY="test-geoapify-key-7f3a")
    def test_the_page_carries_no_secret(self) -> None:
        seed(planobj())

        content = self.client.get(map_path()).content

        self.assertNotIn(b"test-geoapify-key-7f3a", content)
        self.assertNotIn(settings.SECRET_KEY.encode(), content)


class PlanNotFoundTests(SeededMapTestCase):
    """TC-4 (FR-9, NFR-2): unknown, expired and deleted ids are one and the same 404."""

    def _get_missing(self, *, queries: int) -> HttpResponse:
        response = self.assert_read_only(
            lambda: self.client.get(map_path(OTHER_ID)), plan_id=OTHER_ID, queries=queries
        )
        details = assert_envelope(self, response, status=404, code="plan_not_found")
        self.assertEqual(details, {"plan_id": OTHER_ID})
        return response

    def test_an_unknown_an_expired_and_a_deleted_plan_give_the_same_plan_not_found(
        self,
    ) -> None:
        key = planobj_cache_key(OTHER_ID)
        never_stored = self._get_missing(queries=1)

        seed(planobj(), plan_id=OTHER_ID)
        expire_cache_key(key)
        expired = self._get_missing(queries=2)  # the SELECT and Django's DELETE of the row
        self.assertNotIn(key, cache_keys())

        seed(planobj(), plan_id=OTHER_ID)
        cache.delete(key)
        deleted = self._get_missing(queries=1)

        self.assertEqual(never_stored.content, expired.content)
        self.assertEqual(never_stored.content, deleted.content)

    def test_head_on_an_unknown_plan_is_the_json_404(self) -> None:
        response = self.client.head(map_path(OTHER_ID))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")


class BadInputTests(SeededMapTestCase):
    """TC-6 (FR-10, FR-15): non-canonical ids are `not_found`; broken plans are a 500."""

    def test_ids_that_are_not_canonical_uuids_are_the_plain_not_found(self) -> None:
        for plan_id in (PLAN_ID.upper(), PLAN_ID.replace("-", ""), "not-a-uuid"):
            with self.subTest(plan_id=plan_id), self.assertNumQueries(0):
                assert_envelope(
                    self, self.client.get(map_path(plan_id)), status=404, code="not_found"
                )

    def _assert_internal_error(self) -> None:
        with self.assertLogs("fuelplan.views", "ERROR"):
            response = self.client.get(map_path())
        assert_envelope(self, response, status=500, code="internal_error")  # JSON, not HTML
        for leak in (b"KeyError", b"PlanShapeError", b"total_purchase_cost", b"line_cost"):
            self.assertNotIn(leak, response.content)

    def test_a_saved_plan_missing_or_nulling_a_field_is_a_500_not_a_blank_page(self) -> None:
        def without_totals(body: dict) -> None:
            del body["totals"]

        def stop_without_line_cost(body: dict) -> None:
            del body["fuel_stops"][0]["line_cost"]

        def totals_without_total(body: dict) -> None:
            del body["totals"]["total_purchase_cost"]

        def null_total(body: dict) -> None:
            body["totals"]["total_purchase_cost"] = None

        def boolean_gallons(body: dict) -> None:
            body["totals"]["gallons_bought"] = True

        def unknown_purchase_rule(body: dict) -> None:
            body["fuel_stops"][0]["purchase_rule"] = "brim"

        for broken in (
            without_totals,
            stop_without_line_cost,
            totals_without_total,
            null_total,
            boolean_gallons,
            unknown_purchase_rule,
        ):
            with self.subTest(broken.__name__):
                body = planobj()
                broken(body)
                seed(body)
                self._assert_internal_error()

    def test_a_saved_value_that_is_not_a_plan_is_a_500(self) -> None:
        seed("garbage")

        self._assert_internal_error()

    def test_a_database_error_while_reading_the_plan_is_a_500(self) -> None:
        with mock.patch.object(caches["default"], "get", side_effect=DatabaseError("locked")):
            self._assert_internal_error()

    @override_settings(DEBUG=True)
    def test_with_debug_on_a_database_error_is_still_the_json_500_not_a_traceback_page(
        self,
    ) -> None:
        with mock.patch.object(caches["default"], "get", side_effect=DatabaseError("locked")):
            self._assert_internal_error()


class MethodTests(SeededMapTestCase):
    """TC-7 (FR-11, NFR-1): other methods get 01's 405, never a CSRF 403."""

    def test_other_methods_are_the_405_envelope_without_touching_the_plan(self) -> None:
        seed(planobj())
        ttl_before = ttl_of(planobj_cache_key(PLAN_ID))

        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method), self.assertNumQueries(0):
                response = self.client.generic(method, map_path())
                details = assert_envelope(self, response, status=405, code="method_not_allowed")
                self.assertEqual(details, {"allowed": ["GET", "HEAD"]})
                self.assertEqual(response["Allow"], "GET, HEAD")

        self.assertEqual(cache.get(planobj_cache_key(PLAN_ID)), planobj())
        self.assertAlmostEqual(ttl_of(planobj_cache_key(PLAN_ID)), ttl_before, delta=1.0)


class AssetTests(SeededMapTestCase):
    """TC-9 (NFR-6, NFR-8, §8 #3): Leaflet from unpkg with SRI; tiles from OSM over HTTPS."""

    def test_leaflet_comes_from_unpkg_with_sri_before_the_one_inline_script(self) -> None:
        page = self.page_of(planobj())

        links = [attrs for tag, attrs in page.elements if tag == "link"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["href"], LEAFLET_CSS)
        self.assertEqual(links[0]["integrity"], LEAFLET_CSS_SRI)
        self.assertEqual(links[0]["crossorigin"], "")
        kinds = [s.attrs.get("src") or s.attrs.get("type") or "inline" for s in page.scripts]
        self.assertEqual(kinds, ["application/json", LEAFLET_JS, "inline"])
        (leaflet,) = [s.attrs for s in page.scripts if s.attrs.get("src")]
        self.assertEqual(leaflet["integrity"], LEAFLET_JS_SRI)
        self.assertEqual(leaflet["crossorigin"], "")
        self.assertNotIn("async", leaflet)
        self.assertNotIn("defer", leaflet)

    def test_the_only_external_assets_are_leaflet_and_the_only_links_osm_and_geoapify(
        self,
    ) -> None:
        page = self.page_of(planobj())

        asset_tags = {"link", "script", "img", "iframe", "source", "object"}
        assets = [
            attrs.get("src") or attrs.get("href")
            for tag, attrs in page.elements
            if tag in asset_tags and (attrs.get("src") or attrs.get("href"))
        ]
        anchors = {attrs["href"] for tag, attrs in page.elements if tag == "a"}
        self.assertEqual(sorted(assets), [LEAFLET_CSS, LEAFLET_JS])
        self.assertEqual(anchors, {OSM_COPYRIGHT, GEOAPIFY})

    def test_the_script_loads_only_osm_https_tiles_up_to_zoom_19_and_never_prefetches(
        self,
    ) -> None:
        script = self.page_of(planobj()).inline_script

        self.assertIn(TILE_URL, script)
        self.assertIn("maxZoom: 19", script)
        self.assertLessEqual(
            set(re.findall(r"https?://[^\s\"']+", script)), {TILE_URL, OSM_COPYRIGHT}
        )
        for banned in ("serviceWorker", "caches", "fetch(", "prefetch", "XMLHttpRequest", "eval("):
            self.assertNotIn(banned, script)
        self.assertNotIn("{{", script)

    def test_the_script_hands_plan_text_to_leaflet_only_as_text_nodes(self) -> None:
        script = self.page_of(planobj()).inline_script

        self.assertRegex(script, r"html:\s*asText\(point\.label\)")
        self.assertIn("bindPopup(asText(point.title))", script)
        self.assertEqual(re.findall(r"point\.title", script), ["point.title"])

    def test_the_script_shows_the_tiles_failed_message_when_leaflet_or_tiles_fail(self) -> None:
        script = self.page_of(planobj()).inline_script

        self.assertIn("!window.L", script)
        self.assertIn('"tileerror"', script)
        self.assertIn("maxZoom: 13", script)


def hostile_plan(text: str) -> dict:
    """The golden plan with `text` in stop 1's name, address and city, `matched` and a warning."""
    body = planobj()
    stop = body["fuel_stops"][0]
    stop["name"] = stop["address"] = stop["city"] = text
    body["start"]["matched"] = text
    body["warnings"] = [
        {"code": "far_snap", "message": text, "field": "start", "snap_miles": "1.2"}
    ]
    return body


class EscapingTests(SeededMapTestCase):
    """TC-10 (NFR-5): plan text is shown literally and never runs."""

    def test_a_hostile_station_and_place_name_is_shown_verbatim_and_never_runs(self) -> None:
        clean = self.page_of(planobj())
        seed(hostile_plan(HOSTILE))
        response = self.client.get(map_path())
        page = parse_page(response.content)

        self.assertIn(f"1 · {HOSTILE} (OPIS 4021) · {HOSTILE}, {HOSTILE}, IA", page.lines)
        self.assertIn(f"{HOSTILE} → Denver, CO", page.lines)
        self.assertIn(f"S · {HOSTILE} · start with 50.0 gal", page.lines)
        self.assertEqual(page.count(HOSTILE), 1)  # the warning line
        self.assertEqual(page.plan_data["points"][1]["title"], HOSTILE)
        self.assertEqual(page.plan_data["points"][0]["title"], HOSTILE)
        self.assertEqual(page.row_labels(), ["S", "1", "2", "F"])
        self.assertNotIn(b"<img", response.content)
        self.assertNotIn(b"</script><img", response.content)
        self.assertFalse([tag for tag, _ in page.elements if tag == "img"])
        self.assertFalse(
            [name for _, attrs in page.elements for name in attrs if name.startswith("on")]
        )
        self.assertEqual(len(page.scripts), len(clean.scripts))
        self.assertEqual(page.inline_script, clean.inline_script)

    def test_quotes_and_ampersands_survive_both_the_html_and_the_plan_data(self) -> None:
        text = 'O\'Reilly "Fuel" & Go'
        seed(hostile_plan(text))
        response = self.client.get(map_path())
        page = parse_page(response.content)

        self.assertIn(f"1 · {text} (OPIS 4021) · {text}, {text}, IA", page.lines)
        self.assertIn(b"&quot;Fuel&quot;", response.content)
        self.assertIn(b"&amp; Go", response.content)
        self.assertEqual(page.plan_data["points"][1]["title"], text)

    def test_the_template_and_view_never_switch_escaping_off_or_write_html(self) -> None:
        sources = {
            "template": TEMPLATE.read_text(encoding="utf-8"),
            "view": inspect.getsource(views.fuel_plan_map),
            "map_page": Path(map_page.__file__).read_text(encoding="utf-8"),
        }
        banned = ("|safe", "mark_safe", "autoescape off", "innerHTML", "insertAdjacentHTML")
        for name, source in sources.items():
            for word in (*banned, "document.write", "csrf_token"):
                with self.subTest(source=name, word=word):
                    self.assertNotIn(word, source)


class CopyTests(SeededMapTestCase):
    """TC-11 (FR-3, FR-4, FR-5) with the 2026-10-28 amendments."""

    def test_the_header_and_rows_match_the_7b_copy_for_the_golden_plan(self) -> None:
        page = self.page_of(planobj())

        for line in [*GOLDEN_HEADER, *GOLDEN_ROWS, LEGEND, RANGE_LINE]:
            with self.subTest(line=line):
                self.assertEqual(page.count(line), 1)

    def test_the_header_comes_before_the_map_and_the_rows_run_s_1_2_f(self) -> None:
        page = self.page_of(planobj())

        header_positions = [page.index(line) for line in GOLDEN_HEADER]
        row_positions = [page.index(line) for line in GOLDEN_ROWS]
        self.assertEqual(header_positions, sorted(header_positions))
        self.assertLess(header_positions[-1], page.map_index)
        self.assertEqual(row_positions, sorted(row_positions))
        self.assertLessEqual(page.map_index, row_positions[0])  # the rows follow the map

    def test_one_stop_is_written_1_fuel_stop(self) -> None:
        body = planobj()
        body["fuel_stops"] = body["fuel_stops"][:1]

        page = self.page_of(body)

        self.assertIn("Total fuel cost $183.04 · 56 gal bought · 1 fuel stop", page.lines)

    def test_an_approximate_stop_says_check_before_you_go(self) -> None:
        body = planobj()
        body["fuel_stops"][1]["location_quality"] = "approximate"

        page = self.page_of(body)

        self.assertIn("Location: approximate — check before you go", page.lines)
        self.assertIn("Location: poi_match", page.lines)  # stop 1 is not marked


class StoredValueTests(SeededMapTestCase):
    """TC-12 (FR-5): numbers are printed exactly as saved, never re-formatted."""

    def test_values_are_shown_exactly_as_stored(self) -> None:
        body = planobj()
        body["totals"]["total_purchase_cost"] = "1234.50"
        body["route"]["distance_miles"] = "1000.0"
        stop = body["fuel_stops"][0]
        stop.update(price_per_gallon="3.100", line_cost="0.10", fuel_on_arrival_gallons="0.0")

        page = self.page_of(body)

        self.assertIn("Total fuel cost $1234.50 · 56 gal bought · 2 fuel stops", page.lines)
        self.assertIn(
            "Mile 412.6 · arrive with 0.0 gal · buy 41 gal × $3.100 (CSV price) = $0.10 "
            "· fill tank",
            page.lines,
        )
        self.assertIn(
            "Route 1000.0 mi · driven 1005.2 mi incl. detours · finish with 5.4 gal · "
            "burns 100.5 gal",
            page.lines,
        )
        self.assertFalse([line for line in page.lines if "1,234" in line])


class PointTests(SeededMapTestCase):
    """TC-13 (FR-2): the script gets the stored geometry and S, 1…n, F as lat/lon."""

    def test_plan_data_holds_the_stored_geometry_and_the_points_as_lat_lon(self) -> None:
        data = self.page_of(planobj()).plan_data

        self.assertEqual(data["geometry"], GOLDEN_BODY["route"]["geometry"])
        self.assertEqual(
            [(p["label"], p["lat"], p["lon"]) for p in data["points"]],
            [
                ("S", 41.85003, -87.65005),
                ("1", 41.48, -95.22),
                ("2", 40.62554, -103.20771),
                ("F", 39.73915, -104.9847),
            ],
        )

    def test_an_end_without_a_matched_place_is_named_by_its_lat_lon(self) -> None:
        for end, header, row in (
            (
                "start",
                "41.85003, -87.65005 → Denver, CO",
                "S · 41.85003, -87.65005 · start with 50.0 gal",
            ),
            (
                "finish",
                "Chicago, IL → 39.73915, -104.9847",
                "F · 39.73915, -104.9847 · arrive with 5.4 gal",
            ),
        ):
            with self.subTest(end=end):
                body = planobj()
                body[end]["matched"] = None

                page = self.page_of(body)

                self.assertIn(header, page.lines)
                self.assertIn(row, page.lines)


class OrderTests(SeededMapTestCase):
    """TC-14 (FR-3): stops keep the stored order; equal names are told apart by OPIS id."""

    def test_stops_keep_their_stored_order_and_twins_differ_by_opis_id(self) -> None:
        body = planobj()
        third = copy.deepcopy(body["fuel_stops"][1])
        third.update(sequence=3, station_id="8800", price_per_gallon="2.001", line_cost="1.00")
        body["fuel_stops"][0]["name"] = "ZZZ LAST BY NAME"
        body["fuel_stops"].append(third)

        page = self.page_of(body)

        twin = "EXAMPLE TRAVEL CENTER #2"
        positions = [
            page.index("S · Chicago, IL · start with 50.0 gal"),
            page.index("1 · ZZZ LAST BY NAME (OPIS 4021) · I-80, EXIT 46, Walnut, IA"),
            page.index(f"2 · {twin} (OPIS 7310) · I-76, EXIT 125, Sterling, CO"),
            page.index(f"3 · {twin} (OPIS 8800) · I-76, EXIT 125, Sterling, CO"),
            page.index("F · Denver, CO · arrive with 5.4 gal"),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(page.row_labels(), ["S", "1", "2", "3", "F"])


def notice_lines(page: ParsedPage) -> list[str]:
    """Any detours notice, whatever numbers it carries (a zero-stop plan has a 0.0 margin)."""
    return [line for line in page.lines if line.startswith("Detours are straight-line")]


def next_leg_lines(page: ParsedPage) -> list[str]:
    return [line for line in page.lines if line.startswith("Next leg ")]


class DetoursNoticeTests(SeededMapTestCase):
    """TC-15 (FR-7, FR-3): the estimate notice and the per-stop "(estimated)" mark."""

    def test_unverified_detours_show_the_notice_once_above_the_map(self) -> None:
        page = self.page_of(planobj())

        self.assertEqual(page.count(DETOURS_NOTICE), 1)
        self.assertLess(page.index(DETOURS_NOTICE), page.map_index)

    def test_a_mixed_plan_shows_the_notice_and_marks_only_the_unverified_stop(self) -> None:
        body = planobj()
        body["fuel_stops"][0]["detour_verified"] = True

        page = self.page_of(body)

        self.assertEqual(page.count(DETOURS_NOTICE), 1)
        self.assertEqual(
            next_leg_lines(page),
            [
                "Next leg 433.0 mi · ~1.2 mi extra driving (off and back)",
                "Next leg 159.0 mi · ~0.8 mi extra driving (off and back) (estimated)",
            ],
        )

    def test_all_verified_detours_show_no_notice_and_no_estimated_mark(self) -> None:
        body = planobj()
        body["detours_verified"] = True
        for stop in body["fuel_stops"]:
            stop["detour_verified"] = True

        page = self.page_of(body)

        self.assertEqual(notice_lines(page), [])
        self.assertFalse([line for line in next_leg_lines(page) if "(estimated)" in line])


class WarningsAndNotesTests(SeededMapTestCase):
    """TC-16 (FR-6) and amendment 4: warnings under the notice, above the map."""

    def test_every_warning_and_note_appears_once_in_stored_order(self) -> None:
        body = planobj()
        body["warnings"] = [
            {"code": "approximate_location", "message": APPROXIMATE_WARNING, "station_id": "7310"},
            {
                "code": "far_snap",
                "message": FAR_SNAP_WARNING,
                "field": "start",
                "snap_miles": "1.2",
            },
        ]

        page = self.page_of(body)

        stored = [APPROXIMATE_WARNING, FAR_SNAP_WARNING, *body["notes"]]
        for line in stored:
            with self.subTest(line=line):
                self.assertEqual(page.count(line), 1)
        positions = [page.index(line) for line in stored]
        self.assertEqual(positions, sorted(positions))

    def test_warnings_sit_under_the_detours_notice_and_above_the_map(self) -> None:
        body = planobj()
        body["warnings"] = [{"code": "far_snap", "message": FAR_SNAP_WARNING, "field": "start"}]

        page = self.page_of(body)

        self.assertLess(page.index(DETOURS_NOTICE), page.index(FAR_SNAP_WARNING))
        self.assertLess(page.index(FAR_SNAP_WARNING), page.map_index)


def zero_stop_plan() -> dict:
    body = planobj()
    body["fuel_stops"] = []
    body["totals"].update(gallons_bought=0, total_purchase_cost="0.00")
    body["route"]["planning_margin_miles"] = "0.0"
    return body


class ZeroStopTests(SeededMapTestCase):
    """TC-17 (FR-8), seeded half: no stop, no notice, S and F only."""

    def test_a_zero_stop_plan_shows_the_zero_stop_message_and_no_detours_notice(self) -> None:
        body = zero_stop_plan()

        page = self.page_of(body)

        self.assertEqual(page.count(ZERO_STOP), 1)
        self.assertIn("Total fuel cost $0.00 · 0 gal bought · 0 fuel stops", page.lines)
        self.assertEqual(notice_lines(page), [])
        self.assertEqual(page.row_labels(), ["S", "F"])
        self.assertFalse([line for line in page.lines if line.startswith("Mile ")])
        for note in body["notes"]:
            self.assertIn(note, page.lines)

    def test_a_plan_with_stops_has_no_zero_stop_message(self) -> None:
        page = self.page_of(planobj())

        self.assertNotIn(ZERO_STOP, page.lines)


class AttributionTests(SeededMapTestCase):
    """TC-18 (FR-13): OSM, Geoapify and the stored sources are credited."""

    def test_the_page_credits_osm_geoapify_and_every_stored_source(self) -> None:
        seed(planobj())
        response = self.client.get(map_path())
        page = parse_page(response.content)

        self.assertIn("© OpenStreetMap contributors", page.lines)
        self.assertIn(
            b'\xc2\xa9 <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
            b"contributors",
            response.content,
        )
        self.assertIn(
            b'Powered by <a href="https://www.geoapify.com/">Geoapify</a>', response.content
        )
        for item in GOLDEN_BODY["assumptions"]["attribution"]:
            self.assertEqual(page.count(item), 1)
        self.assertIn("Station data version: sha256:ab12…", page.lines)
        self.assertIn(OSM_COPYRIGHT, page.inline_script)  # the tile layer's own credit


class NoJavaScriptTests(SeededMapTestCase):
    """TC-19 (FR-12): every fact is server HTML; the fallback copy is there once."""

    def test_all_plan_text_is_there_with_the_scripts_removed(self) -> None:
        seed(planobj())
        content = self.client.get(map_path()).content
        without_scripts = re.sub(rb"<script\b.*?</script>", b"", content, flags=re.DOTALL)

        page = parse_page(without_scripts)

        self.assertFalse(page.scripts)
        expected = [*GOLDEN_HEADER, *GOLDEN_ROWS, LEGEND, RANGE_LINE, DETOURS_NOTICE]
        expected += [*GOLDEN_BODY["notes"], *GOLDEN_BODY["assumptions"]["attribution"]]
        expected += ["© OpenStreetMap contributors", "Powered by Geoapify"]
        for line in expected:
            with self.subTest(line=line):
                self.assertIn(line, page.lines)

    def test_the_noscript_and_tiles_failed_copy_say_on_this_page(self) -> None:
        page = self.page_of(planobj())

        self.assertEqual(" ".join(page.noscript.split()), NOSCRIPT)
        tile_errors = [attrs for tag, attrs in page.elements if attrs.get("id") == "tile-error"]
        self.assertEqual(tile_errors, [{"id": "tile-error", "hidden": None}])
        self.assertEqual(page.count(TILES_FAILED), 1)
        self.assertEqual(page.index(TILES_FAILED) + 1, page.index(NOSCRIPT))


class PlannerToMapTests(ApiTestCase):
    """TC-2 (FR-1, FR-5, NFR-9) and TC-17's start = finish case, through the real planner."""

    def setUp(self) -> None:
        super().setUp()
        self.harness = PlannerHarness(index=golden_index())
        patcher = self.harness.patch_view()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_map_of_a_just_posted_plan_shows_the_post_json_values(self) -> None:
        self.harness.queue_fixture("chicago_denver")
        body = json_body(self.post_json(GOLDEN_REQUEST))
        self.assertEqual(self.harness.transport.count, 1)

        response = self.client.get(body["map_url"])
        page = parse_page(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.harness.transport.count, 1)
        totals, route = body["totals"], body["route"]
        expected = [
            f"Total fuel cost ${totals['total_purchase_cost']} · {totals['gallons_bought']} gal "
            f"bought · {len(body['fuel_stops'])} fuel stops",
            f"Route {route['distance_miles']} mi · driven {route['driven_distance_miles']} mi "
            f"incl. detours · finish with {body['finish_fuel_on_arrival_gallons']} gal · "
            f"burns {totals['fuel_burned_gallons']} gal",
            f"Station data version: {body['assumptions']['dataset_version']}",
        ]
        for stop in body["fuel_stops"]:
            expected += [
                f"{stop['sequence']} · {stop['name']} (OPIS {stop['station_id']}) · "
                f"{stop['address']}, {stop['city']}, {stop['state']}",
                f"Mile {stop['route_mile']} · arrive with {stop['fuel_on_arrival_gallons']} gal"
                f" · buy {stop['gallons_to_buy']} gal × ${stop['price_per_gallon']} (CSV price)"
                f" = ${stop['line_cost']} · {RULE_WORDS[stop['purchase_rule']]}",
                f"Next leg {stop['miles_to_next']} mi · ~{stop['estimated_detour_miles']} mi "
                "extra driving (off and back) (estimated)",
                f"Location: {stop['location_quality']}",
            ]
        for line in expected:
            with self.subTest(line=line):
                self.assertEqual(page.count(line), 1)

    def test_a_start_equal_to_finish_plan_opens_with_only_s_and_f(self) -> None:
        point = {"latitude": 41.85003, "longitude": -87.65005}
        body = json_body(self.post_json({"start": point, "finish": point}))

        response = self.client.get(body["map_url"])
        page = parse_page(response.content)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.harness.transport.count, 0)
        self.assertEqual(page.row_labels(), ["S", "F"])
        self.assertIn(ZERO_STOP, page.lines)
        self.assertEqual(notice_lines(page), [])
