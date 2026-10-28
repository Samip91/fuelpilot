"""Both ends of `POST /api/v1/fuel-plans`: strict request validation and the response body.

Request (spec 01 §7a, FR-1, FR-5, FR-6): DRF's own `FloatField` and `CharField` are too
lenient here: they accept "41.85" and `true` as numbers and turn 123 into "123". The small
fields below accept exactly the JSON types the spec allows, and every message names the
fix. Unknown keys are reported one by one instead of being silently dropped.

Response (§7a, FR-24 to FR-31, I4): the planner hands over unrounded Decimals; this is the
only place they are rounded to the shown strings, each value exactly once.
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Literal

from rest_framework import serializers

from .errors import BODY_KEY, InvalidRequest, JsonValue, NoFeasiblePlan
from .geo import MAX_DETOUR_MILES, LonLat, miles_from_meters
from .locations import ResolvedEndpoint, normalise_state
from .map_page import MIN_STOPS_FOR_ROUNDING_NOTE, MONEY_NOTE
from .optimizer import (
    MAX_RESERVE_GALLONS,
    MILES_PER_GALLON,
    PLANNING_TANK_GALLONS,
    TANK_GALLONS,
    FuelGap,
    FuelPlan,
    PlannedStop,
    after_purchase_range_miles,
    first_leg_range_miles,
    hard_max_range_miles,
)
from .regions import KNOWN_REGION_CODES
from .stations import StationIndex

CITY_MAX_CHARS = 100  # NFR-10
STATE_MAX_CHARS = 10  # anything longer is not a code; "Illinois" still gets the code hint
FUEL_STEP = Decimal("0.1")
DEFAULT_CURRENT_GALLONS = Decimal("50.0")
DEFAULT_RESERVE_GALLONS = Decimal("5.0")
FUEL_STRING_MAX_CHARS = 16
FUEL_STRING_PATTERN = re.compile(r"[0-9]+(\.[0-9]+)?", re.ASCII)

CITY_FORM = ("city", "state")
POINT_FORM = ("latitude", "longitude")
FORMS_TEXT = "city + state or latitude + longitude"
ONE_FORM_MESSAGE = f"Send either {FORMS_TEXT}."
BOTH_FORMS_MESSAGE = f"Send either {FORMS_TEXT}, not both."


@dataclass(frozen=True, slots=True)
class CityInput:
    """An endpoint given by name; `state` is trimmed but otherwise as sent."""

    city: str
    state: str


@dataclass(frozen=True, slots=True)
class PointInput:
    """An endpoint given by coordinates, already checked to be finite and in range."""

    latitude: float
    longitude: float


type EndpointInput = CityInput | PointInput


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """A validated request; fuel values are Decimal at 0.1 and never negative zero."""

    start: EndpointInput
    finish: EndpointInput
    current_fuel_gallons: Decimal
    reserve_gallons: Decimal


def _unknown_key_message(allowed: tuple[str, ...]) -> str:
    return f"Unknown field. Allowed: {', '.join(allowed)}."


class StrictTextField(serializers.Field):
    """A JSON string only, trimmed and not blank, at most `max_length` characters."""

    default_error_messages = {
        "required": "This field is required.",
        "null": "Must be a string, not null.",
        "invalid": "Must be a string.",
        "blank": "Must not be blank.",
        "too_long": "At most {max_length} characters.",
    }

    def __init__(self, *, max_length: int, **kwargs: object) -> None:
        self.max_length = max_length
        super().__init__(**kwargs)

    def to_internal_value(self, data: object) -> str:
        if not isinstance(data, str):
            self.fail("invalid")
        text = data.strip()
        if not text:
            self.fail("blank")
        if len(text) > self.max_length:
            self.fail("too_long", max_length=self.max_length)
        return text

    def to_representation(self, value: str) -> str:
        return value


class CoordinateField(serializers.Field):
    """A JSON number only (never a string or a boolean), finite and inside [low, high]."""

    default_error_messages = {
        "required": "This field is required.",
        "null": "Must be a JSON number, not null.",
        "invalid": "Must be a JSON number.",
        "string": "Must be a JSON number, not a string.",
        "not_finite": "Must be a finite number.",
        "out_of_range": "Must be between {low:g} and {high:g}.",
    }

    def __init__(self, *, low: float, high: float, **kwargs: object) -> None:
        self.low = low
        self.high = high
        super().__init__(**kwargs)

    def to_internal_value(self, data: object) -> float:
        if isinstance(data, str):
            self.fail("string")
        # bool is a subclass of int, so it has to be refused before the number check.
        if isinstance(data, bool) or not isinstance(data, int | float):
            self.fail("invalid")
        try:
            value = float(data)
        except OverflowError:  # a huge JSON integer
            self.fail("not_finite")
        if not math.isfinite(value):  # 1e400 parses to inf
            self.fail("not_finite")
        if not self.low <= value <= self.high:
            self.fail("out_of_range", low=self.low, high=self.high)
        return value

    def to_representation(self, value: float) -> float:
        return value


class FuelGallonsField(serializers.Field):
    """Gallons as a JSON number or a plain numeric string, ≤ 1 decimal, in [0, maximum].

    Numbers go through `Decimal(repr(x))`, so 12.3 stays 12.3 instead of a 50-digit
    binary float. The result is quantised to 0.1 and made non-negative, so JSON -0.0
    and 0 give the same value (and later the same plan key).
    """

    default_error_messages = {
        "null": "Must be a number such as 12.5, not null.",
        "invalid": 'Must be a number or a numeric string such as "12.5".',
        "not_finite": "Must be a finite number.",
        "precision": "At most 1 decimal place (e.g. 12.5).",
        "out_of_range": "Must be between 0 and {maximum}.",
    }

    def __init__(self, *, maximum: Decimal, **kwargs: object) -> None:
        self.maximum = maximum
        super().__init__(**kwargs)

    def to_internal_value(self, data: object) -> Decimal:
        value = self._as_decimal(data)
        if not value.is_finite():
            self.fail("not_finite")
        if value.normalize().as_tuple().exponent < -1:  # 12.50 is fine, 12.55 is not
            self.fail("precision")
        if not Decimal("0") <= value <= self.maximum:
            self.fail("out_of_range", maximum=self.maximum)
        return value.quantize(FUEL_STEP).copy_abs()

    def _as_decimal(self, data: object) -> Decimal:
        if isinstance(data, bool):
            self.fail("invalid")
        if isinstance(data, int):
            return Decimal(data)
        if isinstance(data, float):
            return Decimal(repr(data))
        is_plain_number = (
            isinstance(data, str)
            and len(data) <= FUEL_STRING_MAX_CHARS
            and FUEL_STRING_PATTERN.fullmatch(data) is not None
        )
        if not is_plain_number:  # also refuses "NaN", "1e2", "12,5" and ""
            self.fail("invalid")
        return Decimal(data)

    def to_representation(self, value: Decimal) -> str:
        return str(value)


class RejectUnknownKeysMixin:
    """Report every key the serializer does not define, as an error on that key's path."""

    def to_internal_value(self, data: object) -> object:
        unknown_errors: dict[str, list[str]] = {}
        if isinstance(data, Mapping):
            allowed = tuple(self.fields)
            unknown_errors = {
                str(key): [_unknown_key_message(allowed)] for key in data if key not in self.fields
            }
        try:
            value = super().to_internal_value(data)
        except serializers.ValidationError as error:
            raise serializers.ValidationError({**error.detail, **unknown_errors}) from None
        if unknown_errors:
            raise serializers.ValidationError(unknown_errors)
        return value


class EndpointSerializer(RejectUnknownKeysMixin, serializers.Serializer):
    """`start` or `finish`: exactly {city, state} or {latitude, longitude} (FR-1)."""

    default_error_messages = {
        "required": f"Required: an object with {FORMS_TEXT}.",
        "null": f"Must be an object with {FORMS_TEXT}, not null.",
        "invalid": f"Must be an object with {FORMS_TEXT}.",
    }

    city = StrictTextField(max_length=CITY_MAX_CHARS, required=False)
    state = StrictTextField(max_length=STATE_MAX_CHARS, required=False)
    latitude = CoordinateField(low=-90.0, high=90.0, required=False)
    longitude = CoordinateField(low=-180.0, high=180.0, required=False)

    def validate_state(self, value: str) -> str:
        """Only real codes pass; AK, HI or ON pass here and are refused later as 422 (FR-3/4)."""
        code = normalise_state(value)
        if code in KNOWN_REGION_CODES:
            return value
        # "XX" has the right shape but is no state; "Illinois" or "ILL" has the wrong shape.
        is_code_shaped = len(code) == 2 and code.isascii() and code.isalpha()
        if is_code_shaped:
            raise serializers.ValidationError(
                f'"{value}" is not a US state code; use a 2-letter USPS code, e.g. IL.'
            )
        raise serializers.ValidationError(
            f'"{value}" is not a 2-letter state code; use the USPS code, e.g. IL.'
        )

    def validate(self, attrs: dict[str, object]) -> EndpointInput:
        city_keys = [key for key in CITY_FORM if key in attrs]
        point_keys = [key for key in POINT_FORM if key in attrs]
        if city_keys and point_keys:
            raise serializers.ValidationError(
                {key: [BOTH_FORMS_MESSAGE] for key in _extra_form_keys(city_keys, point_keys)}
            )
        if not city_keys and not point_keys:
            raise serializers.ValidationError(ONE_FORM_MESSAGE)
        form = CITY_FORM if city_keys else POINT_FORM
        missing = [key for key in form if key not in attrs]
        if missing:
            raise serializers.ValidationError(
                {key: [f"Required with {', '.join(attrs)}. {ONE_FORM_MESSAGE}"] for key in missing}
            )
        if form == CITY_FORM:
            return CityInput(city=attrs["city"], state=attrs["state"])
        return PointInput(latitude=attrs["latitude"], longitude=attrs["longitude"])


def _extra_form_keys(city_keys: list[str], point_keys: list[str]) -> list[str]:
    """With both forms mixed, the keys to blame: those of the form that is not complete.

    If both or neither are complete, the coordinates are blamed, because city + state
    is the form the README shows first.
    """
    is_point_complete = len(point_keys) == len(POINT_FORM)
    is_city_complete = len(city_keys) == len(CITY_FORM)
    if is_point_complete and not is_city_complete:
        return city_keys
    return point_keys


class PlanRequestSerializer(RejectUnknownKeysMixin, serializers.Serializer):
    """The whole request body; `plan_request` is the validated value object."""

    start = EndpointSerializer()
    finish = EndpointSerializer()
    current_fuel_gallons = FuelGallonsField(maximum=TANK_GALLONS, default=DEFAULT_CURRENT_GALLONS)
    reserve_gallons = FuelGallonsField(maximum=MAX_RESERVE_GALLONS, default=DEFAULT_RESERVE_GALLONS)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        current, reserve = attrs["current_fuel_gallons"], attrs["reserve_gallons"]
        if current < reserve:
            # Name the default when it was not sent, or the caller sees a value they never set.
            is_reserve_sent = "reserve_gallons" in self.initial_data
            reserve_label = f"({reserve})" if is_reserve_sent else f"(default {reserve})"
            raise serializers.ValidationError(
                {
                    "reserve_gallons": [
                        f"reserve_gallons {reserve_label} must not be more than "
                        f"current_fuel_gallons ({current})."
                    ]
                }
            )
        return attrs

    @property
    def plan_request(self) -> PlanRequest:
        """The validated request; only valid after `is_valid()` returned True."""
        data = self.validated_data
        return PlanRequest(
            start=data["start"],
            finish=data["finish"],
            current_fuel_gallons=data["current_fuel_gallons"],
            reserve_gallons=data["reserve_gallons"],
        )


def parse_plan_request(data: object) -> PlanRequest:
    """Validate a parsed JSON body; raises InvalidRequest or DRF ValidationError (both 400)."""
    if not isinstance(data, dict):  # [], "x", 1 and null are not requests
        raise InvalidRequest({BODY_KEY: ["The body must be a JSON object."]})
    serializer = PlanRequestSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.plan_request


# --- The response body (spec 01 §7a) -----------------------------------------------------

TENTH = Decimal("0.1")
CENT = Decimal("0.01")
POSITION_DECIMALS = 5  # GeoJSON positions: JSON numbers at 5 dp, [lon, lat]
FAR_SNAP_MILES = Decimal("1.0")  # FR-42: warn only when the shown value is above this

PRICE_RULE = "lowest listed CSV price per station among rows with a resolved location"
PRICE_SOURCE = "provided CSV snapshot, not live"
ATTRIBUTION = [
    "Routing: OSRM, © OpenStreetMap contributors (ODbL)",
    "Station locations: Geoapify, © OpenStreetMap contributors (ODbL)",
    "Places: GeoNames (CC BY 4.0)",
    "Boundaries: US Census Bureau",
]
DETOUR_NOTE = (
    "Detours are the extra miles to leave the route, reach the station and come back, "
    "estimated as twice the straight-line distance (not measured on roads), so a real leg "
    "can be slightly longer than planned; keep a reserve."
)
PRICE_NOTE = "Prices are the provided CSV snapshot, not live."
ARRIVAL_ROUNDING_NOTE = (
    "Fuel on arrival is rounded down and fuel burned to the nearest 0.1, so start + bought "
    "− burned can differ from the finish figure by 0.1 gal."
)
WHOLE_GALLON_NOTE = (
    "Fill stops buy whole gallons rounded down (the brim takes under 1 gallon more); "
    "other stops buy just enough, rounded up."
)
MERGED_STOP_NOTE = (
    "A planned stop was skipped: the whole gallons bought at the previous stop already "
    "reach the next stop or the finish."
)
MERGED_LEG_NOTE = (  # spec 01 §15, whole-gallon replay
    "A leg can be longer than the after_purchase planning range because a stop was merged "
    "during rounding; it stays within hard_max."
)
SAME_POINT_NOTE = (
    "Start and finish are the same point (to 5 decimal places), so there is no route to "
    "drive and no fuel to buy."
)
NO_USABLE_FUEL_MESSAGE = (
    "Cannot plan this trip among the stations checked: start fuel equals reserve_gallons, "
    "so the truck has no usable fuel to leave the start."
)

DetourMethod = Literal["straight_line_estimate", "measured", "mixed"]


def miles_text(miles: Decimal) -> str:
    """Miles as shown: 0.1, ROUND_HALF_UP (§7a)."""
    return str(miles.quantize(TENTH, rounding=ROUND_HALF_UP))


def arrival_text(gallons: Decimal) -> str:
    """Fuel on arrival, rounded down to 0.1, so shown arrival + bought never passes 50."""
    return str(gallons.quantize(TENTH, rounding=ROUND_DOWN))


def gap_leg_text(miles: Decimal) -> str:
    """`gap_leg_miles` rounded up, so it never looks short enough to fit the range (AC-18)."""
    return str(miles.quantize(TENTH, rounding=ROUND_CEILING))


def money_text(amount: Decimal) -> str:
    """Dollars as shown: 0.01, ROUND_HALF_UP (FR-30)."""
    return str(amount.quantize(CENT, rounding=ROUND_HALF_UP))


def gallons_text(gallons: Decimal) -> str:
    """Echoed fuel inputs and fuel burned: 0.1, ROUND_HALF_UP ("50.0")."""
    return str(gallons.quantize(TENTH, rounding=ROUND_HALF_UP))


def snap_miles(snap_m: float) -> Decimal:
    """OSRM's snap distance as shown: rounded once from the raw metres (FR-42)."""
    return Decimal(repr(miles_from_meters(snap_m))).quantize(TENTH, rounding=ROUND_HALF_UP)


def position(point: LonLat) -> list[float]:
    """A GeoJSON position [lon, lat] at 5 dp; the only floats in the body (RFC 7946)."""
    lon, lat = point
    return [round(lon, POSITION_DECIMALS), round(lat, POSITION_DECIMALS)]


def point_json(point: LonLat) -> dict[str, JsonValue]:
    return {"type": "Point", "coordinates": position(point)}


def endpoint_echo(
    *, endpoint: EndpointInput, resolved: ResolvedEndpoint, point: LonLat
) -> dict[str, JsonValue]:
    """FR-7: the input as sent, the matched place (null for coordinates) and the 5-dp point."""
    if isinstance(endpoint, CityInput):
        sent: dict[str, JsonValue] = {"city": endpoint.city, "state": endpoint.state}
    else:
        sent = {"latitude": endpoint.latitude, "longitude": endpoint.longitude}
    return {"input": sent, "matched": resolved.matched, "location": point_json(point)}


@dataclass(frozen=True, slots=True)
class PlanFacts:
    """Everything the saved plan body is built from; all miles, gallons and money unrounded.

    `snap_m` is None when start = finish, because then no route was fetched.
    """

    plan_id: str
    start_echo: dict[str, JsonValue]
    finish_echo: dict[str, JsonValue]
    route_miles: Decimal
    geometry: tuple[LonLat, ...]
    snap_m: tuple[float, float] | None
    plan: FuelPlan
    stations: StationIndex
    start_gallons: Decimal
    reserve_gallons: Decimal
    algorithm_version: str
    is_same_point: bool


def plan_body(facts: PlanFacts) -> dict[str, JsonValue]:
    """The saved plan: the §7a body without `map_url` and `meta` (planobj, spec 02 §6)."""
    plan = facts.plan
    detour_method = _detour_method(plan)
    return {
        "plan_id": facts.plan_id,
        "start": facts.start_echo,
        "finish": facts.finish_echo,
        "route": {
            "distance_miles": miles_text(facts.route_miles),
            "driven_distance_miles": miles_text(plan.driven_distance_miles),
            "planning_margin_miles": miles_text(plan.planning_margin_miles),
            "geometry": {
                "type": "LineString",
                "coordinates": [position(point) for point in facts.geometry],
            },
        },
        "fuel_stops": [
            _stop_json(stop, sequence=sequence, stations=facts.stations)
            for sequence, stop in enumerate(plan.stops, start=1)
        ],
        "finish_fuel_on_arrival_gallons": arrival_text(plan.finish_arrival_gallons),
        "totals": {
            "currency": "USD",
            "gallons_bought": plan.gallons_bought,
            "fuel_burned_gallons": gallons_text(plan.driven_distance_miles / MILES_PER_GALLON),
            # The unrounded sum of the lines, rounded once (FR-30).
            "total_purchase_cost": money_text(plan.total_cost),
        },
        "assumptions": _assumptions(facts, detour_method=detour_method),
        "detours_verified": detour_method == "measured",
        "notes": _notes(facts, detour_method=detour_method),
        "warnings": _warnings(facts),
    }


def response_body(
    *, saved: Mapping[str, JsonValue], map_url: str, meta: Mapping[str, JsonValue]
) -> dict[str, JsonValue]:
    """The §7a order: `plan_id`, `map_url`, `summary`, the saved fields, `meta`, then `route`.

    `saved` already carries this request's start/finish echo (the planner merges it on a
    plan hit), so the summary always names the places this request sent. The summary and
    `meta` are built per response and never saved; `route` goes last because its geometry
    is long and would push everything else out of sight.
    """
    fields = {key: value for key, value in saved.items() if key not in ("plan_id", "route")}
    return {
        "plan_id": saved["plan_id"],
        "map_url": map_url,
        "summary": summary_block(saved),
        **fields,
        "meta": dict(meta),
        "route": saved["route"],
    }


# --- The human-readable summary (spec 01 amendment 2026-10-28, response readability) ------

# Per-stop reasons. A price claim is made only when the next kept stop's price shows it:
# after the whole-gallon replay merges a stop, the stop that really follows is not always
# the one the optimiser compared against, so otherwise the neutral wording is used.
FILL_DEARER_NEXT_TEXT = "fill up: the next stop costs more"
FILL_TEXT = "fill up"
JUST_ENOUGH_CHEAPER_NEXT_TEXT = "just enough to reach the next stop at the same or a lower price"
JUST_ENOUGH_TEXT = "just enough to reach the next stop"
TO_FINISH_TEXT = "enough to reach the finish"
KNOWN_PURCHASE_RULES = frozenset({"fill_tank", "to_next_stop", "to_finish"})
APPROXIMATE_STOP_TEXT = " Town-level location: check the exact exit before you go."
# Only with 2+ stops: with one stop its line cost is the total, so nothing can differ.
ROUNDING_TEXT = f" {MONEY_NOTE}"


class UnknownPurchaseRule(Exception):
    """A stop's `purchase_rule` has no summary text: a bug, left unhandled → logged 500."""


def summary_block(body: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """`headline`, one line per stop and `finish`, in plain words for a person to read.

    Pure: it only re-prints values already in the body (strings rounded for display), so
    the summary can never disagree with the fields below it.
    """
    start, finish = _place_label(body["start"]), _place_label(body["finish"])
    return {
        "headline": _headline(body, start=start, finish=finish),
        "stops": _stop_lines(body["fuel_stops"]),
        "finish": (
            f"Arrive in {finish} with {body['finish_fuel_on_arrival_gallons']} gal left. "
            f"The start tank ({body['assumptions']['start_fuel_gallons']} gal) is not charged."
        ),
    }


def _place_label(endpoint: Mapping[str, JsonValue]) -> str:
    """The matched place name, else "lat, lon" from the stored [lon, lat], as the map shows."""
    if endpoint["matched"] is not None:
        return endpoint["matched"]
    lon, lat = endpoint["location"]["coordinates"]
    return f"{lat}, {lon}"


def _headline(body: Mapping[str, JsonValue], *, start: str, finish: str) -> str:
    trip = f"{start} → {finish}: {body['route']['distance_miles']} mi"
    totals = body["totals"]
    stop_count = len(body["fuel_stops"])
    if not stop_count:
        return (
            f"{trip}, no fuel stop needed — the start tank covers this trip "
            f"(${totals['total_purchase_cost']})."
        )
    stops_text = "1 fuel stop" if stop_count == 1 else f"{stop_count} fuel stops"
    rounding_text = ROUNDING_TEXT if stop_count >= MIN_STOPS_FOR_ROUNDING_NOTE else ""
    return (
        f"{trip}, {stops_text}, total fuel cost ${totals['total_purchase_cost']} "
        f"for {totals['gallons_bought']} gal.{rounding_text}"
    )


def _stop_lines(stops: list[Mapping[str, JsonValue]]) -> list[str]:
    """One line per stop; each reason looks at the stop that really comes next (or none)."""
    last = len(stops) - 1
    return [
        _stop_line(stop, next_stop=stops[index + 1] if index < last else None)
        for index, stop in enumerate(stops)
    ]


def _reason_text(
    stop: Mapping[str, JsonValue], *, next_stop: Mapping[str, JsonValue] | None
) -> str:
    """Why this stop buys what it buys, claiming only what the next kept stop's price shows."""
    rule = stop["purchase_rule"]
    if rule not in KNOWN_PURCHASE_RULES:
        raise UnknownPurchaseRule(f"no summary text for purchase_rule {rule!r}")
    if rule == "to_finish":
        return TO_FINISH_TEXT
    # Both prices are the CSV text as stored, so comparing them adds no rounding.
    next_price = None if next_stop is None else Decimal(next_stop["price_per_gallon"])
    price = Decimal(stop["price_per_gallon"])
    if rule == "fill_tank":
        is_next_dearer = next_price is not None and next_price > price
        return FILL_DEARER_NEXT_TEXT if is_next_dearer else FILL_TEXT
    is_next_cheaper_or_equal = next_price is not None and next_price <= price
    return JUST_ENOUGH_CHEAPER_NEXT_TEXT if is_next_cheaper_or_equal else JUST_ENOUGH_TEXT


def _stop_line(stop: Mapping[str, JsonValue], *, next_stop: Mapping[str, JsonValue] | None) -> str:
    reason = _reason_text(stop, next_stop=next_stop)
    line = (
        f"{stop['sequence']}. Mile {stop['route_mile']} — {stop['name']}, {stop['city']}, "
        f"{stop['state']} ({stop['address']}): arrive with {stop['fuel_on_arrival_gallons']} "
        f"gal, buy {stop['gallons_to_buy']} gal × ${stop['price_per_gallon']} = "
        f"${stop['line_cost']} — {reason}."
    )
    if stop["location_quality"] == "approximate":
        line += APPROXIMATE_STOP_TEXT
    return line


def _stop_json(stop: PlannedStop, *, sequence: int, stations: StationIndex) -> dict[str, JsonValue]:
    """FR-25: one stop; miles and arrival are driven values, the price is the CSV text."""
    candidate = stop.candidate
    record = stations.record(candidate.station_id)
    return {
        "sequence": sequence,
        "station_id": str(record.station_id),
        "name": record.name,
        "address": record.address,
        "city": record.city,
        "state": record.state,
        "location": point_json((record.longitude, record.latitude)),
        "route_mile": miles_text(candidate.route_mile),
        "estimated_detour_miles": miles_text(candidate.detour_miles),
        "fuel_on_arrival_gallons": arrival_text(stop.arrival_gallons),
        "purchase_rule": stop.purchase_rule,
        "gallons_to_buy": stop.gallons_to_buy,
        "price_per_gallon": record.price_text,
        "line_cost": money_text(stop.line_cost),
        "miles_to_next": miles_text(stop.driven_miles_to_next),
        "location_quality": record.location_quality,
        "detour_verified": candidate.is_detour_verified,
    }


def _detour_method(plan: FuelPlan) -> DetourMethod:
    """FR-27: "measured" only if there are stops and every one was verified."""
    flags = [stop.candidate.is_detour_verified for stop in plan.stops]
    if flags and all(flags):
        return "measured"
    if any(flags):
        return "mixed"
    return "straight_line_estimate"


def _assumptions(facts: PlanFacts, *, detour_method: DetourMethod) -> dict[str, JsonValue]:
    """FR-31: every planning constant, so the numbers can be checked by hand."""
    reserve = facts.reserve_gallons
    first_leg = first_leg_range_miles(start_gallons=facts.start_gallons, reserve_gallons=reserve)
    return {
        "mpg": str(MILES_PER_GALLON),
        "tank_gallons": str(TANK_GALLONS),
        "planning_tank_gallons": str(PLANNING_TANK_GALLONS),
        "reserve_gallons": gallons_text(reserve),
        "start_fuel_gallons": gallons_text(facts.start_gallons),
        "start_fuel_charged": False,
        "effective_range_miles": {
            "first_leg": miles_text(first_leg),
            "after_purchase": miles_text(after_purchase_range_miles(reserve)),
            "hard_max": miles_text(hard_max_range_miles(reserve)),
        },
        "max_detour_miles": miles_text(MAX_DETOUR_MILES),
        "unverified_stop_margin_miles": miles_text(facts.plan.stop_margin_miles),
        "detour_method": detour_method,
        "price_rule": PRICE_RULE,
        "price_source": PRICE_SOURCE,
        "optimality": _optimality(facts.plan, detour_method=detour_method),
        "dataset_version": facts.stations.dataset_version,
        "algorithm_version": facts.algorithm_version,
        "attribution": list(ATTRIBUTION),
    }


def _optimality(plan: FuelPlan, *, detour_method: DetourMethod) -> str:
    """The optimality claim names the margin and detour kind actually used (FR-27)."""
    detours = {
        "straight_line_estimate": "straight-line detours",
        "measured": "measured detours",
        "mixed": "measured and straight-line detours",
    }[detour_method]
    model = ["49-gal planning tank"]
    if plan.stop_margin_miles:
        model.append(f"{plan.stop_margin_miles}-mi margin per stop end")
    model += [detours, "candidates ≤ 5 mi"]
    return (
        f"lowest cost within the planning model ({', '.join(model)}) for exact amounts; "
        "purchases rounded to whole gallons (at most 1 extra gallon per stop)"
    )


def _notes(facts: PlanFacts, *, detour_method: DetourMethod) -> list[JsonValue]:
    """FR-26/29/30 notes in a fixed order; optional ones appear only when they apply."""
    plan = facts.plan
    notes: list[JsonValue] = []
    if facts.is_same_point:
        notes.append(SAME_POINT_NOTE)
    elif not plan.stops:
        notes.append(
            f"No fuel stop is needed: the start tank ({gallons_text(facts.start_gallons)} gal) "
            f"covers the {miles_text(facts.route_miles)}-mile trip with "
            f"{arrival_text(plan.finish_arrival_gallons)} gal left."
        )
    # The detour and whole-gallon notes explain stops; the money note needs 2+ line costs.
    has_stops = bool(plan.stops)
    if has_stops and detour_method != "measured":
        notes.append(DETOUR_NOTE)
    if plan.planning_margin_miles:  # omitted at 0.0 (FR-27)
        notes.append(
            f"The plan keeps {miles_text(plan.planning_margin_miles)} extra miles of fuel as a "
            "safety margin for unverified detours; these miles are not driven."
        )
    notes.append(
        f"Start tank ({gallons_text(facts.start_gallons)} gal) is not charged; "
        f"{arrival_text(plan.finish_arrival_gallons)} gal left at finish."
    )
    notes.append(PRICE_NOTE)
    if len(plan.stops) >= MIN_STOPS_FOR_ROUNDING_NOTE:
        notes.append(MONEY_NOTE)
    notes.append(ARRIVAL_ROUNDING_NOTE)
    if has_stops:
        notes.append(WHOLE_GALLON_NOTE)
    if plan.has_merged_leg:
        notes += [MERGED_STOP_NOTE, MERGED_LEG_NOTE]
    return notes


def _warnings(facts: PlanFacts) -> list[JsonValue]:
    """FR-42 far snaps (start, then finish), then FR-28 approximate stops in stop order."""
    warnings: list[JsonValue] = []
    if facts.snap_m is not None:
        for field_name, verb, meters in zip(
            ("start", "finish"), ("starts", "ends"), facts.snap_m, strict=True
        ):
            shown = snap_miles(meters)
            if shown > FAR_SNAP_MILES:
                warnings.append(
                    {
                        "code": "far_snap",
                        "message": (
                            f"The {field_name} point is {shown} mi from the nearest road; the "
                            f"route {verb} there, and those miles are not counted."
                        ),
                        "field": field_name,
                        "snap_miles": str(shown),
                    }
                )
    for stop in facts.plan.stops:
        record = facts.stations.record(stop.candidate.station_id)
        if record.location_quality == "approximate":
            warnings.append(
                {
                    "code": "approximate_location",
                    "message": (
                        f"Station {record.station_id} ({record.name}, {record.city} "
                        f"{record.state}) has only a town-level location; its real position "
                        "and detour may differ."
                    ),
                    "station_id": str(record.station_id),
                }
            )
    return warnings


def no_feasible_plan_error(
    *, gap: FuelGap, route_miles: Decimal, first_leg_miles: Decimal
) -> NoFeasiblePlan:
    """FR-23: the 422 with the gap's rounded fields and a message naming both ends of it.

    The gap starts at the start when it is at mile 0 with the first-leg range, and ends at
    the finish when it reaches mile L (a station exactly there reads the same).
    """
    gap_from, gap_to = miles_text(gap.gap_from_mile), miles_text(gap.gap_to_mile)
    leg, max_leg = gap_leg_text(gap.gap_leg_miles), miles_text(gap.max_leg_miles)
    if not gap.max_leg_miles:
        message = NO_USABLE_FUEL_MESSAGE
    else:
        is_from_start = not gap.gap_from_mile and gap.max_leg_miles == first_leg_miles
        origin = "the start" if is_from_start else f"the station at mile {gap_from}"
        is_to_finish = gap.gap_to_mile == route_miles
        target = f"the finish at mile {gap_to}" if is_to_finish else f"a station at mile {gap_to}"
        message = (
            f"Cannot plan this trip among the stations checked: from {origin} the next stop "
            f"({target}) is {leg} planned miles away (including detours and the safety margin), "
            f"but the truck can cover at most {max_leg} miles there."
        )
    return NoFeasiblePlan(
        message=message,
        gap_from_mile=gap_from,
        gap_to_mile=gap_to,
        gap_leg_miles=leg,
        max_leg_miles=max_leg,
        hint=str(gap.hint),
    )
