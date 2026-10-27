"""What the map page shows (spec 02 §7b), built from a saved plan (the planobj entry).

Pure: stdlib only, no Django and no I/O, so the exact page copy is testable on its own.
Every value is printed as stored (FR-5): stored strings unchanged, stored whole numbers
through `str()`. Nothing is recomputed, rounded or localised here or in the template.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

PURCHASE_RULE_TEXT: Mapping[str, str] = {
    "fill_tank": "fill tank",
    "to_next_stop": "just enough",
    "to_finish": "just enough",
}
ROUNDING_LINE = "(each stop cost is rounded to the cent; the total is rounded once)"
ZERO_STOP_MESSAGE = "No fuel stop needed. Total fuel cost $0.00 — the start tank covers this trip."
LOCATION_LEGEND = (
    "Location: poi_match = matched to the station itself · address_match = matched to its "
    "street address · approximate = only the town or road was found."
)
TILES_FAILED_MESSAGE = (
    "Map tiles could not be loaded (offline or the tile server refused the request). "
    "The route details, stops and totals on this page are still correct."
)
NOSCRIPT_MESSAGE = "The map needs JavaScript; all plan details are listed on this page."


class PlanShapeError(Exception):
    """The saved plan lacks a field the page needs, or has the wrong type (FR-15 → 500)."""


@dataclass(frozen=True, slots=True)
class MapRow:
    """One entry of the list beside the map: its marker label and its lines of text."""

    label: str
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MapPage:
    """Everything the template prints, as finished strings; `plan_data` feeds the script."""

    title: str
    header_lines: tuple[str, ...]
    detours_notice: str | None
    warnings: tuple[str, ...]
    zero_stop_message: str | None
    rows: tuple[MapRow, ...]
    notes: tuple[str, ...]
    range_line: str
    attribution: tuple[str, ...]
    dataset_version: str
    plan_data: dict[str, object]
    # Fixed copy, the same on every page; kept here so all §7b text lives in one module.
    legend: ClassVar[str] = LOCATION_LEGEND
    tiles_failed_message: ClassVar[str] = TILES_FAILED_MESSAGE
    noscript_message: ClassVar[str] = NOSCRIPT_MESSAGE


def build_map_page(plan: object) -> MapPage:
    """The page for one saved plan; raises `PlanShapeError` instead of showing a blank value."""
    start, finish = _place_name(_field(plan, "start")), _place_name(_field(plan, "finish"))
    route, assumptions = _field(plan, "route"), _field(plan, "assumptions")
    stops = _items(plan, "fuel_stops")
    start_gallons = _text(assumptions, "start_fuel_gallons")
    finish_gallons = _text(plan, "finish_fuel_on_arrival_gallons")
    has_notice = bool(stops) and not _flag(plan, "detours_verified")  # FR-7
    return MapPage(
        title=f"{start} → {finish}",
        header_lines=_header_lines(
            route=route,
            totals=_field(plan, "totals"),
            assumptions=assumptions,
            finish_gallons=finish_gallons,
            stop_count=len(stops),
        ),
        detours_notice=_detours_notice(route, assumptions) if has_notice else None,
        warnings=tuple(_text(warning, "message") for warning in _items(plan, "warnings")),
        zero_stop_message=None if stops else ZERO_STOP_MESSAGE,
        rows=(
            MapRow(label="S", lines=(f"S · {start} · start with {start_gallons} gal",)),
            *(_stop_row(stop) for stop in stops),
            MapRow(label="F", lines=(f"F · {finish} · arrive with {finish_gallons} gal",)),
        ),
        notes=_texts(plan, "notes"),
        range_line=_range_line(_field(assumptions, "effective_range_miles")),
        attribution=_texts(assumptions, "attribution"),
        dataset_version=_text(assumptions, "dataset_version"),
        plan_data=_plan_data(plan, route=route, start=start, finish=finish, stops=stops),
    )


def _header_lines(
    *, route: object, totals: object, assumptions: object, finish_gallons: str, stop_count: int
) -> tuple[str, ...]:
    stops_text = f"{stop_count} fuel stop" + ("" if stop_count == 1 else "s")
    return (
        f"Total fuel cost ${_text(totals, 'total_purchase_cost')} · "
        f"{_whole(totals, 'gallons_bought')} gal bought · {stops_text}",
        ROUNDING_LINE,
        f"Route {_text(route, 'distance_miles')} mi · "
        f"driven {_text(route, 'driven_distance_miles')} mi incl. detours · "
        f"finish with {finish_gallons} gal · "
        f"burns {_text(totals, 'fuel_burned_gallons')} gal",
        f"Start tank ({_text(assumptions, 'start_fuel_gallons')} gal) is not charged · "
        f"reserve {_text(assumptions, 'reserve_gallons')} gal. "
        "Prices: provided CSV snapshot, not live.",
    )


def _detours_notice(route: object, assumptions: object) -> str:
    return (
        "Detours are straight-line estimates, not measured on roads. Each stop's estimated "
        f"extra driving is at most {_text(assumptions, 'max_detour_miles')} mi; the real drive "
        "off the highway can be longer. The plan keeps "
        f"{_text(assumptions, 'unverified_stop_margin_miles')} mi of extra fuel at each end of "
        f"every leg that has a stop ({_text(route, 'planning_margin_miles')} mi in total). "
        "Stops marked (estimated) are not road-checked."
    )


def _stop_row(stop: object) -> MapRow:
    """The §7b stop lines; the purchase rule word is a label for the stored rule, not a value."""
    sequence = str(_whole(stop, "sequence"))
    rule = _text(stop, "purchase_rule")
    if rule not in PURCHASE_RULE_TEXT:
        raise PlanShapeError(f"unknown purchase_rule {rule!r}")
    estimated = "" if _flag(stop, "detour_verified") else " (estimated)"
    quality = _text(stop, "location_quality")
    check = " — check before you go" if quality == "approximate" else ""
    return MapRow(
        label=sequence,
        lines=(
            f"{sequence} · {_text(stop, 'name')} (OPIS {_text(stop, 'station_id')}) · "
            f"{_text(stop, 'address')}, {_text(stop, 'city')}, {_text(stop, 'state')}",
            f"Mile {_text(stop, 'route_mile')} · "
            f"arrive with {_text(stop, 'fuel_on_arrival_gallons')} gal · "
            f"buy {_whole(stop, 'gallons_to_buy')} gal × ${_text(stop, 'price_per_gallon')} "
            f"(CSV price) = ${_text(stop, 'line_cost')} · {PURCHASE_RULE_TEXT[rule]}",
            f"Next leg {_text(stop, 'miles_to_next')} mi · "
            f"~{_text(stop, 'estimated_detour_miles')} mi extra driving (off and back){estimated}",
            f"Location: {quality}{check}",
        ),
    )


def _range_line(ranges: object) -> str:
    return (
        f"Range: first leg {_text(ranges, 'first_leg')} mi · "
        f"after a purchase {_text(ranges, 'after_purchase')} mi · "
        f"hard max {_text(ranges, 'hard_max')} mi"
    )


def _plan_data(
    plan: object, *, route: object, start: str, finish: str, stops: list[object]
) -> dict[str, object]:
    """What the script draws: the stored geometry and points S, 1…n, F as lat/lon.

    Labels are "S", "F" or the stored whole-number sequence, so no free text ever
    reaches a marker's HTML; titles are shown by the script as plain text only.
    """
    geometry = _field(route, "geometry")
    if not isinstance(geometry, Mapping):
        raise PlanShapeError("route.geometry is not a GeoJSON object")
    points = [_point("S", _field(plan, "start"), title=start)]
    points += [
        _point(str(_whole(stop, "sequence")), stop, title=_text(stop, "name")) for stop in stops
    ]
    points.append(_point("F", _field(plan, "finish"), title=finish))
    return {"geometry": geometry, "points": points}


def _point(label: str, node: object, *, title: str) -> dict[str, object]:
    lat, lon = _lat_lon(node)
    return {"label": label, "lat": lat, "lon": lon, "title": title}


def _place_name(endpoint: object) -> str:
    """The matched place name, else "lat, lon" printed from the stored numbers (FR-2)."""
    matched = _field(endpoint, "matched")
    if matched is None:
        lat, lon = _lat_lon(endpoint)
        return f"{lat}, {lon}"
    if not isinstance(matched, str):
        raise PlanShapeError("matched is neither text nor null")
    return matched


def _lat_lon(node: object) -> tuple[float, float]:
    """(lat, lon) from the node's stored GeoJSON point, whose coordinates are [lon, lat]."""
    coordinates = _items(_field(node, "location"), "coordinates")
    if len(coordinates) != 2:
        raise PlanShapeError("a point does not have exactly two coordinates")
    lon, lat = (_number(value) for value in coordinates)
    return lat, lon


# Typed accessors: a missing key, a null or a wrong type all become PlanShapeError, so a
# stale or broken entry is a logged 500 and never a page that prints "None" (FR-15).


def _field(node: object, key: str) -> object:
    if not isinstance(node, Mapping) or key not in node:
        raise PlanShapeError(f"the saved plan has no field {key!r}")
    return node[key]


def _text(node: object, key: str) -> str:
    value = _field(node, key)
    if not isinstance(value, str):
        raise PlanShapeError(f"field {key!r} is not text")
    return value


def _texts(node: object, key: str) -> tuple[str, ...]:
    values = _items(node, key)
    if not all(isinstance(value, str) for value in values):
        raise PlanShapeError(f"field {key!r} is not a list of text")
    return tuple(values)


def _whole(node: object, key: str) -> int:
    value = _field(node, key)
    if isinstance(value, bool) or not isinstance(value, int):  # bool is a subclass of int
        raise PlanShapeError(f"field {key!r} is not a whole number")
    return value


def _flag(node: object, key: str) -> bool:
    value = _field(node, key)
    if not isinstance(value, bool):
        raise PlanShapeError(f"field {key!r} is not true or false")
    return value


def _items(node: object, key: str) -> list[object]:
    value = _field(node, key)
    if not isinstance(value, list):
        raise PlanShapeError(f"field {key!r} is not a list")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PlanShapeError("a coordinate is not a number")
    return value
