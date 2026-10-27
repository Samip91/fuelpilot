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
