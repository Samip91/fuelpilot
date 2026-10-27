"""Region codes for the service area (lower 48 + DC) and the codes we reject by name."""

LOWER_48_AND_DC: frozenset[str] = frozenset(
    {
        "AL", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "ID", "IL", "IN",
        "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE",
        "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    }
)  # fmt: skip

# Known codes outside the service area: they get "unsupported location", not "bad input".
OUTSIDE_SERVICE_AREA: frozenset[str] = frozenset(
    {"AK", "HI", "PR", "GU", "VI", "AS", "MP"}  # US states and territories
    | {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}  # Canada
)


def is_in_service_area(state_code: str) -> bool:
    """True for the lower 48 states and DC, the only area the planner serves."""
    return state_code.strip().upper() in LOWER_48_AND_DC


# Every code a caller may send: anything else ("XX") is bad input (400), not "unsupported".
KNOWN_REGION_CODES: frozenset[str] = LOWER_48_AND_DC | OUTSIDE_SERVICE_AREA

# (lat_min, lat_max, lon_min, lon_max) around the lower 48 + DC: a cheap sanity check for
# station points, never the service-area test itself (that is the boundary polygon).
LOWER_48_BBOX: tuple[float, float, float, float] = (24.0, 49.5, -125.0, -66.5)


def is_inside_lower_48_bbox(*, latitude: float, longitude: float) -> bool:
    """True if the point lies in the rough box around the lower 48 + DC."""
    lat_min, lat_max, lon_min, lon_max = LOWER_48_BBOX
    return lat_min <= latitude <= lat_max and lon_min <= longitude <= lon_max
