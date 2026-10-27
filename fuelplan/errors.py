"""Domain errors that leave the service layer with a stable code and HTTP status (spec 01 §7a).

The JSON envelope that turns these into responses is added with the views (build 2B).
"""

SERVICE_AREA_SCOPE = "contiguous US + DC"


class FuelPlanError(Exception):
    """Base class: each subclass fixes its §7a `code` and HTTP `status`."""

    code: str = "internal_error"
    status: int = 500

    def __init__(self, message: str, *, details: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, str] = details or {}


class LocationNotFound(FuelPlanError):
    """A valid lower-48/DC state, but no populated place with that name (FR-3)."""

    code = "location_not_found"
    status = 422

    def __init__(self, *, field: str, city: str, state: str, state_code: str) -> None:
        """`state` is echoed as sent in details; `state_code` is its normalised form."""
        super().__init__(
            f"No populated place with that name was found in {state_code} for {field}. "
            "Send coordinates (latitude and longitude) instead.",
            details={"field": field, "city": city, "state": state},
        )


class UnsupportedLocation(FuelPlanError):
    """A state, province or point outside the service area (FR-4, rule 4)."""

    code = "unsupported_location"
    status = 422

    def __init__(self, *, field: str) -> None:
        super().__init__(
            f"The {field} location is outside the service area: {SERVICE_AREA_SCOPE} only.",
            details={"field": field, "scope": SERVICE_AREA_SCOPE},
        )
