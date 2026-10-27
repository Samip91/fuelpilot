"""The detour-verification seam (spec 01 FR-44): one call the planner makes after optimising.

v1 ships only the no-op, which returns `not_tried` and makes no call. Spec 03 (cut for v1)
would replace it with a verifier that measures detours on roads within the call budget.
The planner loads the verifier from the `FUELPILOT_DETOUR_VERIFIER` setting on every
request, so a test can swap it with `override_settings` alone.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

from django.conf import settings
from django.utils.module_loading import import_string

from .geo import RouteLine
from .optimizer import Candidate, PlannedStop
from .routing import CallBudget

VerificationStatus = Literal["not_tried", "verified", "failed"]


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """The seam's answer; `measured_detour_miles` maps station ID → measured detour (0.01 mi)."""

    status: VerificationStatus
    measured_detour_miles: Mapping[int, Decimal] = field(default_factory=dict)


class DetourVerifier(Protocol):
    """What the planner calls; `budget` is the request's routing-call budget."""

    def __call__(
        self,
        *,
        route: RouteLine,
        chosen_stops: Sequence[PlannedStop],
        backups: Sequence[Candidate],
        budget: CallBudget,
    ) -> VerificationResult: ...


def verify_detours_noop(
    *,
    route: RouteLine,
    chosen_stops: Sequence[PlannedStop],
    backups: Sequence[Candidate],
    budget: CallBudget,
) -> VerificationResult:
    """The v1 verifier: measures nothing and spends no routing call."""
    return VerificationResult(status="not_tried")


def load_detour_verifier() -> DetourVerifier:
    """The verifier named by the `FUELPILOT_DETOUR_VERIFIER` setting (a dotted path)."""
    return import_string(settings.FUELPILOT_DETOUR_VERIFIER)
