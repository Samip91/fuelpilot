"""Pure fuel-stop optimiser: cheapest stops, whole-gallon replay and the gap finder.

Spec 01 FR-18 to FR-23, FR-29, FR-44 and §15 "Optimiser (D8)". No Django, no I/O and no
project imports (TC-41), so the planner can call it and the tests can check it on its own.

The model, in planned miles of fuel above the reserve r:
- nodes are the start (mile 0), the candidates sorted by (route_mile, station_id) and the
  finish (mile L);
- a planned leg is the route miles between two nodes plus half the detour at each station
  end plus the stop margin at each station end; the driven leg is the same without margins;
- the truck may leave the start with F = (current - r) x 10 miles and a station with at most
  U = (49 - r) x 10 miles (the 49-gallon planning tank).

All arithmetic runs under an exact Decimal context that traps any rounding, so a total can
never drift; display rounding happens in the serializer, not here.
"""

from bisect import bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal, Inexact, localcontext
from enum import StrEnum
from typing import Literal

MILES_PER_GALLON = Decimal("10")
TANK_GALLONS = Decimal("50")
PLANNING_TANK_GALLONS = Decimal("49")
STOP_MARGIN_MILES = Decimal("1")
MAX_RESERVE_GALLONS = Decimal("48")

# Exact arithmetic: 50 digits is far above the ~17 significant digits a cost can reach
# (prices carry up to 8 decimals), and the Inexact trap turns any hidden rounding into a bug.
_PRECISION_DIGITS = 50
_ZERO = Decimal("0")

PurchaseRule = Literal["fill_tank", "to_next_stop", "to_finish"]
ExactRule = Literal["fill", "just_enough"]


class OptimizerError(Exception):
    """A broken precondition or invariant: a bug, which the planner reports as 500."""


class FeasibilityHint(StrEnum):
    """The closed set of FR-23 hints: only settings that really make the trip feasible."""

    MORE_FUEL = "start with more fuel"
    LOWER_RESERVE = "lower reserve_gallons"
    MORE_FUEL_OR_LOWER_RESERVE = "start with more fuel or lower reserve_gallons"
    MORE_FUEL_AND_LOWER_RESERVE = "start with more fuel and lower reserve_gallons (both needed)"
    NOT_FIXABLE = "no station in the CSV covers this stretch; fuel settings cannot fix it"


@dataclass(frozen=True, slots=True)
class Candidate:
    """A station the truck may stop at; miles are Decimal to 0.01, price is the CSV value."""

    station_id: int
    route_mile: Decimal
    detour_miles: Decimal
    price_per_gallon: Decimal
    is_detour_verified: bool


@dataclass(frozen=True, slots=True)
class ExactStop:
    """One stop of the exact plan: how much the planning model buys there, before rounding."""

    candidate: Candidate
    rule: ExactRule
    gallons: Decimal
    node: int  # position in the sorted route, so the replay needs no lookup


@dataclass(frozen=True, slots=True)
class ExactPlan:
    """The cheapest plan for exact (fractional) amounts, as the DP finds it (FR-19)."""

    cost: Decimal
    stops: tuple[ExactStop, ...]

    @property
    def gallons(self) -> Decimal:
        """Gallons the exact plan buys in total."""
        return sum((stop.gallons for stop in self.stops), _ZERO)


@dataclass(frozen=True, slots=True)
class PlannedStop:
    """One stop of the final plan, after whole-gallon rounding; values are unrounded Decimals."""

    candidate: Candidate
    arrival_gallons: Decimal
    purchase_rule: PurchaseRule
    gallons_to_buy: int
    line_cost: Decimal
    driven_miles_to_next: Decimal
    planned_miles_to_next: Decimal

    @property
    def tank_after_purchase_gallons(self) -> Decimal:
        """Real tank when the truck leaves this stop."""
        return self.arrival_gallons + self.gallons_to_buy


@dataclass(frozen=True, slots=True)
class FuelPlan:
    """A feasible plan: the stops, the real tank series and the money, all unrounded."""

    stops: tuple[PlannedStop, ...]
    stop_margin_miles: Decimal
    planning_margin_miles: Decimal
    driven_distance_miles: Decimal
    finish_arrival_gallons: Decimal
    gallons_bought: int
    total_cost: Decimal
    exact_cost: Decimal
    has_merged_leg: bool


@dataclass(frozen=True, slots=True)
class FuelGap:
    """No feasible plan (FR-23, I7): the stretch no reachable node can cross, and a hint."""

    gap_from_mile: Decimal
    gap_to_mile: Decimal
    gap_leg_miles: Decimal
    max_leg_miles: Decimal
    hint: FeasibilityHint


def stop_margin_miles(candidates: Sequence[Candidate]) -> Decimal:
    """FR-44: 1 mi per stop end unless every candidate is verified; no candidates -> 1 mi."""
    if candidates and all(candidate.is_detour_verified for candidate in candidates):
        return _ZERO
    return STOP_MARGIN_MILES


def first_leg_range_miles(*, start_gallons: Decimal, reserve_gallons: Decimal) -> Decimal:
    """F: planned miles the start tank allows before the reserve (FR-20)."""
    return (start_gallons - reserve_gallons) * MILES_PER_GALLON


def after_purchase_range_miles(reserve_gallons: Decimal) -> Decimal:
    """U: planned miles a 49-gallon planning tank allows after a purchase (FR-20)."""
    return (PLANNING_TANK_GALLONS - reserve_gallons) * MILES_PER_GALLON


def hard_max_range_miles(reserve_gallons: Decimal) -> Decimal:
    """Miles a full 50-gallon tank allows above the reserve; the replay never exceeds it."""
    return (TANK_GALLONS - reserve_gallons) * MILES_PER_GALLON


def plan_fuel_stops(
    *,
    route_miles: Decimal,
    candidates: Sequence[Candidate],
    start_gallons: Decimal,
    reserve_gallons: Decimal,
) -> FuelPlan | FuelGap:
    """Return the cheapest whole-gallon plan, or the gap that makes the trip infeasible.

    Raises `OptimizerError` on invalid input or a broken invariant (both are bugs upstream).
    """
    try:
        with localcontext(_exact_context()):
            graph, ranges = _prepare(
                route_miles=route_miles,
                candidates=candidates,
                start_gallons=start_gallons,
                reserve_gallons=reserve_gallons,
            )
            exact = _exact_plan(graph, ranges)
            if exact is None:
                return _describe_gap(
                    graph, start_gallons=start_gallons, reserve_gallons=reserve_gallons
                )
            return _replay_whole_gallons(
                graph, exact=exact, start_gallons=start_gallons, reserve_gallons=reserve_gallons
            )
    except Inexact as error:
        raise OptimizerError("inexact arithmetic on optimiser input") from error


def find_cheapest_exact_plan(
    *,
    route_miles: Decimal,
    candidates: Sequence[Candidate],
    start_gallons: Decimal,
    reserve_gallons: Decimal,
) -> ExactPlan | None:
    """The DP's exact optimum before whole-gallon rounding, or None if infeasible.

    This is the same DP `plan_fuel_stops` runs; tests compare it with the references (TC-15).
    """
    try:
        with localcontext(_exact_context()):
            graph, ranges = _prepare(
                route_miles=route_miles,
                candidates=candidates,
                start_gallons=start_gallons,
                reserve_gallons=reserve_gallons,
            )
            return _exact_plan(graph, ranges)
    except Inexact as error:
        raise OptimizerError("inexact arithmetic on optimiser input") from error


def _prepare(
    *,
    route_miles: Decimal,
    candidates: Sequence[Candidate],
    start_gallons: Decimal,
    reserve_gallons: Decimal,
) -> tuple["_RouteGraph", "_Ranges"]:
    """Validate the inputs, then build the sorted route graph and the F/U ranges.

    Fails fast on values the serializer and the corridor should already have excluded.
    """
    _require_finite_decimal("route_miles", route_miles)
    _require_finite_decimal("start_gallons", start_gallons)
    _require_finite_decimal("reserve_gallons", reserve_gallons)
    if route_miles < 0:
        raise OptimizerError(f"route_miles must be >= 0, got {route_miles}")
    if not _ZERO <= reserve_gallons <= MAX_RESERVE_GALLONS:
        raise OptimizerError(f"reserve_gallons must be in 0..48, got {reserve_gallons}")
    if not reserve_gallons <= start_gallons <= TANK_GALLONS:
        raise OptimizerError(
            f"start_gallons must be in reserve..50, got {start_gallons} (reserve {reserve_gallons})"
        )
    seen_ids: set[int] = set()
    for candidate in candidates:
        _validate_candidate(candidate, route_miles=route_miles)
        if candidate.station_id in seen_ids:
            raise OptimizerError(f"duplicate station_id {candidate.station_id}")
        seen_ids.add(candidate.station_id)
    graph = _RouteGraph.build(route_miles=route_miles, candidates=candidates)
    ranges = _Ranges.of(start_gallons=start_gallons, reserve_gallons=reserve_gallons)
    return graph, ranges


def _exact_plan(graph: "_RouteGraph", ranges: "_Ranges") -> ExactPlan | None:
    """Zero stops when the start tank covers the trip (FR-29), else the DP's optimum."""
    if graph.miles[graph.finish] <= ranges.first_leg_miles:
        return ExactPlan(cost=_ZERO, stops=())
    return _CheapestPathSearch(graph, ranges).run()


# --- Input checks ------------------------------------------------------------------------


def _validate_candidate(candidate: Candidate, *, route_miles: Decimal) -> None:
    """One candidate: integer id, mile on the route, detour >= 0, positive price."""
    if not isinstance(candidate.station_id, int) or isinstance(candidate.station_id, bool):
        raise OptimizerError(f"station_id must be an int, got {candidate.station_id!r}")
    _require_finite_decimal("route_mile", candidate.route_mile)
    _require_finite_decimal("detour_miles", candidate.detour_miles)
    _require_finite_decimal("price_per_gallon", candidate.price_per_gallon)
    if not _ZERO <= candidate.route_mile <= route_miles:
        raise OptimizerError(f"station {candidate.station_id}: route_mile outside 0..L")
    if candidate.detour_miles < 0:
        raise OptimizerError(f"station {candidate.station_id}: detour_miles < 0")
    if candidate.price_per_gallon <= 0:
        raise OptimizerError(f"station {candidate.station_id}: price_per_gallon <= 0")


def _require_finite_decimal(name: str, value: object) -> None:
    """Floats never enter the money path (NFR-5)."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise OptimizerError(f"{name} must be a finite Decimal, got {value!r}")


def _exact_context() -> Context:
    """A Decimal context in which any rounding raises `decimal.Inexact` (default traps kept)."""
    context = Context(prec=_PRECISION_DIGITS)
    context.traps[Inexact] = True
    return context


# --- The route as a graph of nodes -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RouteGraph:
    """Start, sorted candidates and finish, with the per-node values the legs need.

    Node 0 is the start and the last node is the finish; node k (1..n) is `candidates[k-1]`.
    """

    candidates: tuple[Candidate, ...]
    miles: tuple[Decimal, ...]
    half_detours: tuple[Decimal, ...]
    planned_end_extras: tuple[Decimal, ...]
    prices: tuple[Decimal, ...]
    margin_miles: Decimal

    @classmethod
    def build(cls, *, route_miles: Decimal, candidates: Sequence[Candidate]) -> "_RouteGraph":
        """Sort by (route_mile, numeric station_id) so the result never depends on input order."""
        ordered = tuple(sorted(candidates, key=lambda c: (c.route_mile, c.station_id)))
        margin = stop_margin_miles(ordered)
        half_detours = tuple(c.detour_miles / 2 for c in ordered)
        return cls(
            candidates=ordered,
            miles=(_ZERO, *(c.route_mile for c in ordered), route_miles),
            half_detours=(_ZERO, *half_detours, _ZERO),
            planned_end_extras=(_ZERO, *(half + margin for half in half_detours), _ZERO),
            # The start and finish never sell fuel; 0 is a placeholder that is never read.
            prices=(_ZERO, *(c.price_per_gallon for c in ordered), _ZERO),
            margin_miles=margin,
        )

    @property
    def finish(self) -> int:
        """Index of the finish node."""
        return len(self.miles) - 1

    def candidate_at(self, node: int) -> Candidate:
        """The station at a station node."""
        return self.candidates[node - 1]

    def stop_key(self, node: int) -> tuple[Decimal, int]:
        """The (mile, station_id) a stop contributes to the tie-break sequence."""
        candidate = self.candidates[node - 1]
        return (candidate.route_mile, candidate.station_id)

    def planned_leg(self, origin: int, target: int) -> Decimal:
        """Route miles + d/2 + margin at each station end (FR-18, FR-20, FR-44)."""
        return (
            self.miles[target]
            - self.miles[origin]
            + self.planned_end_extras[origin]
            + self.planned_end_extras[target]
        )

    def driven_leg(self, origin: int, target: int) -> Decimal:
        """Route miles + d/2 at each station end: what the truck really drives."""
        return (
            self.miles[target]
            - self.miles[origin]
            + self.half_detours[origin]
            + self.half_detours[target]
        )

    def legs_within(self, origin: int, range_miles: Decimal) -> Iterator[tuple[Decimal, int]]:
        """(planned leg, target) for every later node within `range_miles` of `origin`.

        A planned leg is never shorter than its route miles, so a bisect on miles bounds
        the scan and keeps it linear in the stations ahead (TC-44).
        """
        window_end = bisect_right(self.miles, self.miles[origin] + range_miles)
        for target in range(origin + 1, window_end):
            leg = self.planned_leg(origin, target)
            if leg <= range_miles:
                yield leg, target


@dataclass(frozen=True, slots=True)
class _Ranges:
    """F out of the start and U out of a station, in planned miles above the reserve."""

    first_leg_miles: Decimal
    after_purchase_miles: Decimal

    @classmethod
    def of(cls, *, start_gallons: Decimal, reserve_gallons: Decimal) -> "_Ranges":
        """The ranges for one (start fuel, reserve) setting."""
        return cls(
            first_leg_miles=first_leg_range_miles(
                start_gallons=start_gallons, reserve_gallons=reserve_gallons
            ),
            after_purchase_miles=after_purchase_range_miles(reserve_gallons),
        )

    def out_of(self, node: int) -> Decimal:
        """The longest planned leg allowed out of a node."""
        return self.first_leg_miles if node == 0 else self.after_purchase_miles


# --- Reachability, the gap and the hint (FR-23, I7) --------------------------------------


def _furthest_reachable_node(graph: _RouteGraph, ranges: _Ranges) -> int:
    """Highest node index reachable over the DP's edges (the finish if the trip is feasible)."""
    is_reached = [False] * len(graph.miles)
    is_reached[0] = True
    for origin in range(graph.finish):
        if not is_reached[origin]:
            continue
        for _leg, target in graph.legs_within(origin, ranges.out_of(origin)):
            is_reached[target] = True
    return max(node for node, reached in enumerate(is_reached) if reached)


def _describe_gap(
    graph: _RouteGraph, *, start_gallons: Decimal, reserve_gallons: Decimal
) -> FuelGap:
    """The first stretch nothing reachable can cross: from the furthest reachable node on.

    Called only when the DP found no plan. The reachable set need not be a prefix, so the
    furthest node (not the first miss) is used.
    """
    ranges = _Ranges.of(start_gallons=start_gallons, reserve_gallons=reserve_gallons)
    gap_from = _furthest_reachable_node(graph, ranges)
    if gap_from == graph.finish:
        raise OptimizerError("the DP found no plan but the finish is reachable")
    gap_to = gap_from + 1
    return FuelGap(
        gap_from_mile=graph.miles[gap_from],
        gap_to_mile=graph.miles[gap_to],
        gap_leg_miles=graph.planned_leg(gap_from, gap_to),
        max_leg_miles=ranges.out_of(gap_from),
        hint=_feasibility_hint(graph, start_gallons=start_gallons, reserve_gallons=reserve_gallons),
    )


def _feasibility_hint(
    graph: _RouteGraph, *, start_gallons: Decimal, reserve_gallons: Decimal
) -> FeasibilityHint:
    """Re-run reachability with (50, r), (current, 0) and (50, 0); name only what works."""

    def is_feasible_with(start: Decimal, reserve: Decimal) -> bool:
        ranges = _Ranges.of(start_gallons=start, reserve_gallons=reserve)
        return _furthest_reachable_node(graph, ranges) == graph.finish

    more_fuel_works = start_gallons < TANK_GALLONS and is_feasible_with(
        TANK_GALLONS, reserve_gallons
    )
    lower_reserve_works = reserve_gallons > 0 and is_feasible_with(start_gallons, _ZERO)
    if more_fuel_works and lower_reserve_works:
        return FeasibilityHint.MORE_FUEL_OR_LOWER_RESERVE
    if more_fuel_works:
        return FeasibilityHint.MORE_FUEL
    if lower_reserve_works:
        return FeasibilityHint.LOWER_RESERVE
    if is_feasible_with(TANK_GALLONS, _ZERO):
        return FeasibilityHint.MORE_FUEL_AND_LOWER_RESERVE
    return FeasibilityHint.NOT_FIXABLE


# --- The exact DP (FR-19, §15) -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Step:
    """How a state was reached: the station it left, the fuel it had there and the rule."""

    node: int
    fuel_miles: Decimal
    rule: ExactRule


@dataclass(frozen=True, slots=True)
class _Label:
    """Best known way to reach a (node, fuel) state; `stops` counts purchases before it."""

    cost: Decimal
    stops: int
    parent: _Step | None


@dataclass(frozen=True, slots=True)
class _Source:
    """A state at the node being expanded, with its sort key computed once.

    key = cost - fuel x price / 10, so the cost at the next node is key + (bought-to) x
    price / 10 for both rules: the best source for an edge is the min key below a threshold.
    """

    fuel_miles: Decimal
    key: Decimal
    stops: int


class _CheapestPathSearch:
    """Forward DP over (node, fuel above reserve) states; nodes are visited in route order.

    From a station i with fuel g to a later node j (planned leg l <= U):
    - just enough when j is the finish or p_j <= p_i: needs g < l, arrives with 0;
    - fill otherwise: needs g < U (never "sell" fuel), arrives with U - l.
    Labels compare (cost, stops, (mile, id) sequence), so ties are deterministic.
    """

    def __init__(self, graph: _RouteGraph, ranges: _Ranges) -> None:
        self._graph = graph
        self._ranges = ranges
        self._states: list[dict[Decimal, _Label]] = [{} for _ in graph.miles]

    def run(self) -> ExactPlan | None:
        """The exact optimum to the finish, or None when no chain of legs reaches it."""
        self._seed_from_start()
        for node in range(1, self._graph.finish):
            if self._states[node]:
                self._expand(node)
        final = self._states[self._graph.finish].get(_ZERO)
        if final is None:
            return None
        return ExactPlan(cost=final.cost, stops=self._exact_stops(final))

    def _seed_from_start(self) -> None:
        """The start does not sell fuel: each station within F is reached at cost 0."""
        first_leg = self._ranges.first_leg_miles
        for leg, target in self._graph.legs_within(0, first_leg):
            if target != self._graph.finish:
                self._states[target][first_leg - leg] = _Label(cost=_ZERO, stops=0, parent=None)

    def _expand(self, node: int) -> None:
        """Relax every edge out of one station using the min-key speed-up (TC-44)."""
        graph = self._graph
        price = graph.prices[node]
        tank_miles = self._ranges.after_purchase_miles
        sources = self._sources_by_fuel(node, price)
        best_fill: _Source | None = None
        for source in sources:
            if source.fuel_miles < tank_miles:
                best_fill = self._better_source(node, best_fill, source)
        fill_cost_offset = tank_miles * price / MILES_PER_GALLON

        finish = graph.finish
        best_just: _Source | None = None
        next_source = 0
        for leg, target in sorted(graph.legs_within(node, tank_miles)):
            if target == finish or graph.prices[target] <= price:
                while next_source < len(sources) and sources[next_source].fuel_miles < leg:
                    best_just = self._better_source(node, best_just, sources[next_source])
                    next_source += 1
                if best_just is not None:
                    cost = best_just.key + leg * price / MILES_PER_GALLON
                    self._offer(
                        target=target,
                        fuel_miles=_ZERO,
                        cost=cost,
                        node=node,
                        source=best_just,
                        rule="just_enough",
                    )
            elif best_fill is not None:
                cost = best_fill.key + fill_cost_offset
                self._offer(
                    target=target,
                    fuel_miles=tank_miles - leg,
                    cost=cost,
                    node=node,
                    source=best_fill,
                    rule="fill",
                )

    def _sources_by_fuel(self, node: int, price: Decimal) -> list[_Source]:
        """The node's states sorted by fuel, each key computed once (the profile hot spot)."""
        return [
            _Source(
                fuel_miles=fuel,
                key=label.cost - fuel * price / MILES_PER_GALLON,
                stops=label.stops,
            )
            for fuel, label in sorted(self._states[node].items())
        ]

    def _better_source(self, node: int, current: _Source | None, other: _Source) -> _Source:
        """Lower (key, stops) wins; on an exact tie the earlier (mile, id) sequence wins."""
        if current is None:
            return other
        if (other.key, other.stops) != (current.key, current.stops):
            return other if (other.key, other.stops) < (current.key, current.stops) else current
        states = self._states[node]
        if self._sequence_of(states[other.fuel_miles]) < self._sequence_of(
            states[current.fuel_miles]
        ):
            return other
        return current

    def _offer(
        self,
        *,
        target: int,
        fuel_miles: Decimal,
        cost: Decimal,
        node: int,
        source: _Source,
        rule: ExactRule,
    ) -> None:
        """Keep the new label at (target, fuel) if it beats the one already there.

        Most offers lose on cost alone, so that check runs before anything is built.
        """
        old = self._states[target].get(fuel_miles)
        stops = source.stops + 1
        if old is not None:
            if (old.cost, old.stops) < (cost, stops):
                return
            if (old.cost, old.stops) == (cost, stops):
                # Exact ties are rare, so the (mile, id) sequences are only built here.
                new_sequence = (
                    *self._sequence_of(self._states[node][source.fuel_miles]),
                    self._graph.stop_key(node),
                )
                if new_sequence >= self._sequence_of(old):
                    return
        step = _Step(node=node, fuel_miles=source.fuel_miles, rule=rule)
        self._states[target][fuel_miles] = _Label(cost=cost, stops=stops, parent=step)

    def _steps_of(self, label: _Label) -> list[_Step]:
        """The purchases that led to a label, oldest first (the one parent walk)."""
        steps = []
        step = label.parent
        while step is not None:
            steps.append(step)
            step = self._states[step.node][step.fuel_miles].parent
        steps.reverse()
        return steps

    def _sequence_of(self, label: _Label) -> tuple[tuple[Decimal, int], ...]:
        """(mile, id) of the stops a label passed through, oldest first."""
        return tuple(self._graph.stop_key(step.node) for step in self._steps_of(label))

    def _exact_stops(self, final: _Label) -> tuple[ExactStop, ...]:
        """Price each purchase on the path to the finish."""
        steps = self._steps_of(final)
        next_nodes = [s.node for s in steps[1:]] + [self._graph.finish]
        stops = []
        for step, next_node in zip(steps, next_nodes, strict=True):
            if step.rule == "fill":
                bought_miles = self._ranges.after_purchase_miles - step.fuel_miles
            else:
                bought_miles = self._graph.planned_leg(step.node, next_node) - step.fuel_miles
            stops.append(
                ExactStop(
                    candidate=self._graph.candidate_at(step.node),
                    rule=step.rule,
                    gallons=bought_miles / MILES_PER_GALLON,
                    node=step.node,
                )
            )
        return tuple(stops)


# --- Whole-gallon replay (FR-20 to FR-22, §15) -------------------------------------------


@dataclass(frozen=True, slots=True)
class _TankRun:
    """One pass of the real tank; `droppable_index` names a stop that buys nothing."""

    stops: tuple[PlannedStop, ...]
    finish_arrival_gallons: Decimal
    droppable_index: int | None


def _replay_whole_gallons(
    graph: _RouteGraph, *, exact: ExactPlan, start_gallons: Decimal, reserve_gallons: Decimal
) -> FuelPlan:
    """Buy whole gallons on one real tank series (driven miles), dropping zero-buy stops.

    With no stops this is the FR-29 plan: nothing bought, the start tank covers the trip.

    Fill rounds down (the brim takes under 1 gallon more), just-enough rounds up. A stop
    whose purchase rounds to 0 is merged into its predecessor's leg, then the replay re-runs
    until no stop is dropped; the predecessor keeps its amount rule. The FR-20 to FR-22
    invariants are checked on the result before it is returned.
    """
    route = list(exact.stops)
    has_merged_leg = False
    while True:
        run = _run_tank(
            graph, route=route, start_gallons=start_gallons, reserve_gallons=reserve_gallons
        )
        if run.droppable_index is None:
            break
        del route[run.droppable_index]
        has_merged_leg = True

    detour_miles = sum((stop.candidate.detour_miles for stop in run.stops), _ZERO)
    plan = FuelPlan(
        stops=run.stops,
        stop_margin_miles=graph.margin_miles,
        # Each kept stop has two margin-bearing ends (FR-44, uniform margins).
        planning_margin_miles=2 * graph.margin_miles * len(run.stops),
        driven_distance_miles=graph.miles[graph.finish] + detour_miles,
        finish_arrival_gallons=run.finish_arrival_gallons,
        gallons_bought=sum(stop.gallons_to_buy for stop in run.stops),
        total_cost=sum((stop.line_cost for stop in run.stops), _ZERO),
        exact_cost=exact.cost,
        has_merged_leg=has_merged_leg,
    )
    _check_plan_invariants(
        graph,
        plan=plan,
        nodes=[0, *(stop.node for stop in route), graph.finish],
        exact=exact,
        start_gallons=start_gallons,
        reserve_gallons=reserve_gallons,
    )
    return plan


def _run_tank(
    graph: _RouteGraph,
    *,
    route: Sequence[ExactStop],
    start_gallons: Decimal,
    reserve_gallons: Decimal,
) -> _TankRun:
    """Drive the route once; stop early at the first stop that should be dropped."""
    tank_gallons = start_gallons
    previous = 0
    stops: list[PlannedStop] = []
    for index, route_stop in enumerate(route):
        node = route_stop.node
        next_node = route[index + 1].node if index + 1 < len(route) else graph.finish
        arrival = tank_gallons - graph.driven_leg(previous, node) / MILES_PER_GALLON
        planned_to_next = graph.planned_leg(node, next_node)
        gallons = _gallons_to_buy(
            rule=route_stop.rule,
            arrival_gallons=arrival,
            planned_miles_to_next=planned_to_next,
            reserve_gallons=reserve_gallons,
        )
        if gallons <= 0:
            # Merge only if the previous tank still reaches the next node with r + margin/10.
            merged_driven = graph.driven_leg(previous, next_node)
            merged_margin = graph.planned_leg(previous, next_node) - merged_driven
            merged_arrival = tank_gallons - merged_driven / MILES_PER_GALLON
            if merged_arrival >= reserve_gallons + merged_margin / MILES_PER_GALLON:
                return _TankRun(
                    stops=tuple(stops), finish_arrival_gallons=_ZERO, droppable_index=index
                )
            raise OptimizerError(f"stop at node {node} buys {gallons} gal and cannot be merged")
        candidate = graph.candidate_at(node)
        stops.append(
            PlannedStop(
                candidate=candidate,
                arrival_gallons=arrival,
                purchase_rule=_purchase_rule(route_stop.rule, next_node=next_node, graph=graph),
                gallons_to_buy=gallons,
                line_cost=gallons * candidate.price_per_gallon,
                driven_miles_to_next=graph.driven_leg(node, next_node),
                planned_miles_to_next=planned_to_next,
            )
        )
        tank_gallons = arrival + gallons
        previous = node
    finish_arrival = tank_gallons - graph.driven_leg(previous, graph.finish) / MILES_PER_GALLON
    return _TankRun(stops=tuple(stops), finish_arrival_gallons=finish_arrival, droppable_index=None)


def _gallons_to_buy(
    *,
    rule: ExactRule,
    arrival_gallons: Decimal,
    planned_miles_to_next: Decimal,
    reserve_gallons: Decimal,
) -> int:
    """Fill: floor(50 - arrival). Just enough: ceil(r + planned/10 - arrival), never past 50."""
    room = _floor(TANK_GALLONS - arrival_gallons)
    if rule == "fill":
        return room
    needed = reserve_gallons + planned_miles_to_next / MILES_PER_GALLON - arrival_gallons
    return min(_ceil(needed), room)


def _purchase_rule(rule: ExactRule, *, next_node: int, graph: _RouteGraph) -> PurchaseRule:
    """Fill stays fill_tank after a merge (owner 2026-10-28); only just-enough is relabelled."""
    if rule == "fill":
        return "fill_tank"
    return "to_finish" if next_node == graph.finish else "to_next_stop"


def _floor(value: Decimal) -> int:
    """Whole gallons rounded down; to_integral_value never signals Inexact."""
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _ceil(value: Decimal) -> int:
    """Whole gallons rounded up; to_integral_value never signals Inexact."""
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _check_plan_invariants(
    graph: _RouteGraph,
    *,
    plan: FuelPlan,
    nodes: Sequence[int],
    exact: ExactPlan,
    start_gallons: Decimal,
    reserve_gallons: Decimal,
) -> None:
    """FR-20 to FR-22 and AC-16/17 on the final plan; any failure is a bug (OptimizerError).

    `nodes` is the kept route: start, the final stops, finish.
    """
    hard_max = hard_max_range_miles(reserve_gallons)
    tanks_leaving = [start_gallons, *(stop.tank_after_purchase_gallons for stop in plan.stops)]
    arrivals = [*(stop.arrival_gallons for stop in plan.stops), plan.finish_arrival_gallons]

    for stop in plan.stops:
        if stop.gallons_to_buy < 1:
            raise OptimizerError(f"station {stop.candidate.station_id} buys < 1 gallon")
        if stop.arrival_gallons < 0 or stop.tank_after_purchase_gallons > TANK_GALLONS:
            raise OptimizerError(f"station {stop.candidate.station_id}: tank outside 0..50")

    for leg_index, (origin, target) in enumerate(zip(nodes, nodes[1:], strict=False)):
        planned = graph.planned_leg(origin, target)
        driven = graph.driven_leg(origin, target)
        leaving = tanks_leaving[leg_index]
        range_miles = (leaving - reserve_gallons) * MILES_PER_GALLON
        if not planned <= range_miles <= hard_max:
            raise OptimizerError(f"leg {leg_index}: planned {planned} mi exceeds the tank")
        if arrivals[leg_index] < reserve_gallons + (planned - driven) / MILES_PER_GALLON:
            raise OptimizerError(f"leg {leg_index}: arrival below reserve + margin")

    price_sum = sum((stop.candidate.price_per_gallon for stop in plan.stops), _ZERO)
    if plan.total_cost - plan.exact_cost > price_sum:
        raise OptimizerError("rounded cost exceeds the exact cost by more than 1 gal per stop")
    if plan.gallons_bought - exact.gallons > len(plan.stops):
        raise OptimizerError("rounding bought more than 1 extra gallon per stop")
