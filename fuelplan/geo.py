"""Pure geometry helpers (no Django, no I/O); floats are fine here, never on the money path."""

import math

EARTH_RADIUS_MILES = 3958.7613  # mean Earth radius, 6371.0088 km
KM_PER_MILE = 1.609344


def haversine_miles(*, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in degrees, in miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = phi2 - phi1
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))
