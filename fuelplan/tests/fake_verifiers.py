"""Fake FR-44 verifiers for TC-46, reached only via `FUELPILOT_DETOUR_VERIFIER` (a dotted path).

They measure the two TC-45 golden stations (4021 and 7310) and record what the planner
passed in, so a test can check that v1 sends no backups.
"""

from collections.abc import Sequence
from decimal import Decimal

from fuelplan.geo import RouteLine
from fuelplan.optimizer import Candidate, PlannedStop
from fuelplan.routing import CallBudget
from fuelplan.verification import VerificationResult

received_backups: list[list[Candidate]] = []


def _record(backups: Sequence[Candidate]) -> None:
    received_backups.append(list(backups))


def verify_every_candidate(
    *,
    route: RouteLine,
    chosen_stops: Sequence[PlannedStop],
    backups: Sequence[Candidate],
    budget: CallBudget,
) -> VerificationResult:
    """Both stations measured: 1.0 mi and 0.6 mi."""
    _record(backups)
    return VerificationResult(
        status="verified", measured_detour_miles={4021: Decimal("1.0"), 7310: Decimal("0.6")}
    )


def verify_first_stop_only(
    *,
    route: RouteLine,
    chosen_stops: Sequence[PlannedStop],
    backups: Sequence[Candidate],
    budget: CallBudget,
) -> VerificationResult:
    """Only station 4021 measured (1.0 mi); 7310 keeps its estimate."""
    _record(backups)
    return VerificationResult(status="verified", measured_detour_miles={4021: Decimal("1.0")})


def verify_with_a_far_detour(
    *,
    route: RouteLine,
    chosen_stops: Sequence[PlannedStop],
    backups: Sequence[Candidate],
    budget: CallBudget,
) -> VerificationResult:
    """Station 4021 is really 5.2 mi off the route, so it stops being a candidate."""
    _record(backups)
    return VerificationResult(
        status="verified", measured_detour_miles={4021: Decimal("5.2"), 7310: Decimal("0.6")}
    )


def verify_failed(
    *,
    route: RouteLine,
    chosen_stops: Sequence[PlannedStop],
    backups: Sequence[Candidate],
    budget: CallBudget,
) -> VerificationResult:
    """Verification was tried and failed: the plan is answered but never reused."""
    _record(backups)
    return VerificationResult(status="failed")
