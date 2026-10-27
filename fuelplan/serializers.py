"""Strict request validation for `POST /api/v1/fuel-plans` (spec 01 §7a, FR-1, FR-5, FR-6).

DRF's own `FloatField` and `CharField` are too lenient here: they accept "41.85" and
`true` as numbers and turn 123 into "123". The small fields below accept exactly the
JSON types the spec allows, and every message names the fix. Unknown keys are reported
one by one instead of being silently dropped.
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from rest_framework import serializers

from .errors import BODY_KEY, InvalidRequest
from .locations import normalise_state
from .regions import KNOWN_REGION_CODES

CITY_MAX_CHARS = 100  # NFR-10
STATE_MAX_CHARS = 10  # anything longer is not a code; "Illinois" still gets the code hint
FUEL_STEP = Decimal("0.1")
TANK_GALLONS = Decimal("50")
MAX_RESERVE_GALLONS = Decimal("48")  # Q-5: below the 49-gal planning tank
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
        if normalise_state(value) not in KNOWN_REGION_CODES:
            raise serializers.ValidationError(
                f"{value} is not a US state code; use a 2-letter USPS code such as IL."
            )
        return value

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
            raise serializers.ValidationError(
                {
                    "reserve_gallons": [
                        f"reserve_gallons ({reserve}) must not be more than "
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
