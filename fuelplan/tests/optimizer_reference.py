"""Test-only references for the optimiser (spec 01 TC-15, TC-18, TC-44; not collected as tests).

Independence rule: everything here computes legs, F and U itself from §15 and never calls
the optimiser's helpers, so one formula bug cannot hide in both. Only the `Candidate` and
`FuelPlan` value objects are shared, as plain data.

- `brute_force_plan` is Ref A: all 2^n subsets (n <= 8), each priced by the fixed-path greedy.
- `plain_dp_plan` is the §15 DP with no speed-up and full sequence tuples in its labels; it
  replaces the cut Ref B (owner 2026-10-28) and checks the speed-up on larger instances.
- `any_amount_cost` lets every stop buy any whole number of planned miles (0 included) on
  whole-mile instances; it checks the fill / just-enough rule itself, the job of the cut Ref B.
- `replay_violations` re-checks a final plan's FR-20 to FR-22 properties from scratch.
- The generators build seeded instances from integers, never from floats.
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    localcontext,
)
from itertools import combinations

from fuelplan.optimizer import Candidate, ExactPlan, FuelPlan, PlannedStop

GENERAL_SEED = 20261027
DROP_FOCUSED_SEED = 20261028
PLAIN_DP_SEED = 20261029
PERF_SEED = 20261027
ANY_AMOUNT_SEED = 20261030

_TEN = Decimal("10")
_ZERO = Decimal("0")
_PRICE_POOL = ("2.999", "3.000", "3.199", "3.459", "3.500", "3.899", "4.000", "4.250")

StopSequence = tuple[tuple[Decimal, int], ...]  # (route_mile, station_id) per stop


@dataclass(frozen=True, slots=True)
class Instance:
    """One optimiser input, with the seed that built it (shown in assertion messages)."""

    seed: int
    route_miles: Decimal
    candidates: tuple[Candidate, ...]
    start_gallons: Decimal
    reserve_gallons: Decimal

    def with_fuel(self, *, start_gallons: Decimal, reserve_gallons: Decimal) -> "Instance":
        """The same trip and stations with other fuel settings (the FR-23 hint passes)."""
        return Instance(
            seed=self.seed,
            route_miles=self.route_miles,
            candidates=self.candidates,
            start_gallons=start_gallons,
            reserve_gallons=reserve_gallons,
        )

    def as_kwargs(self) -> dict[str, object]:
        """Keyword arguments for `plan_fuel_stops` / `find_cheapest_exact_plan`."""
        return {
            "route_miles": self.route_miles,
            "candidates": self.candidates,
            "start_gallons": self.start_gallons,
            "reserve_gallons": self.reserve_gallons,
        }


@dataclass(frozen=True, slots=True)
class ReferencePlan:
    """What the references decide: exact cost and the (mile, station_id) stop sequence."""

    cost: Decimal
    sequence: StopSequence


def _exact_context() -> Context:
    """Same guarantee as the optimiser: any rounding raises."""
    return Context(prec=50, traps=[Inexact, InvalidOperation, DivisionByZero, Overflow])


# --- The §15 model, written out again ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Node:
    mile: Decimal
    detour_miles: Decimal
    price: Decimal
    station_id: int
    is_station: bool


class _Model:
    """Nodes, margin, F and U for one instance, computed from §15 directly."""

    def __init__(self, instance: Instance) -> None:
        ordered = sorted(instance.candidates, key=lambda c: (c.route_mile, c.station_id))
        all_verified = bool(ordered) and all(c.is_detour_verified for c in ordered)
        self.margin = _ZERO if all_verified else Decimal("1")
        start = _Node(_ZERO, _ZERO, _ZERO, 0, is_station=False)
        finish = _Node(instance.route_miles, _ZERO, _ZERO, 0, is_station=False)
        stations = [
            _Node(c.route_mile, c.detour_miles, c.price_per_gallon, c.station_id, is_station=True)
            for c in ordered
        ]
        self.nodes = [start, *stations, finish]
        self.finish = len(self.nodes) - 1
        self.reserve = instance.reserve_gallons
        self.first_range = (instance.start_gallons - instance.reserve_gallons) * _TEN
        self.after_purchase_range = (Decimal("49") - instance.reserve_gallons) * _TEN

    def driven(self, i: int, j: int) -> Decimal:
        a, b = self.nodes[i], self.nodes[j]
        return b.mile - a.mile + a.detour_miles / 2 + b.detour_miles / 2

    def margin_of(self, i: int, j: int) -> Decimal:
        return self.margin * sum(1 for k in (i, j) if self.nodes[k].is_station)

    def planned(self, i: int, j: int) -> Decimal:
        return self.driven(i, j) + self.margin_of(i, j)

    def key(self, i: int) -> tuple[Decimal, int]:
        return (self.nodes[i].mile, self.nodes[i].station_id)


# --- Ref A: brute force over subsets ------------------------------------------------------


def brute_force_plan(instance: Instance) -> ReferencePlan | None:
    """Ref A (§15): every subset, fixed-path greedy, "cheaper" = <=, zero-buy subsets rejected."""
    with localcontext(_exact_context()):
        model = _Model(instance)
        best: tuple[Decimal, int, StopSequence] | None = None
        stations = range(1, model.finish)
        for size in range(len(stations) + 1):
            for subset in combinations(stations, size):
                cost = _greedy_cost(model, path=(0, *subset, model.finish))
                if cost is None:
                    continue
                label = (cost, size, tuple(model.key(i) for i in subset))
                if best is None or label < best:
                    best = label
        return None if best is None else ReferencePlan(cost=best[0], sequence=best[2])


def _greedy_cost(model: _Model, *, path: Sequence[int]) -> Decimal | None:
    """Cost of one fixed stop sequence, or None if a leg is too long or a stop buys nothing."""
    legs = [model.planned(a, b) for a, b in zip(path, path[1:], strict=False)]
    if legs[0] > model.first_range or any(leg > model.after_purchase_range for leg in legs[1:]):
        return None
    fuel = model.first_range - legs[0]
    cost = _ZERO
    for position in range(1, len(path) - 1):
        here = path[position]
        price = model.nodes[here].price
        reach = _ZERO
        target: Decimal | None = None
        for ahead in range(position, len(path) - 1):
            reach += legs[ahead]
            if reach > model.after_purchase_range:
                break
            nxt = path[ahead + 1]
            if nxt == model.finish or model.nodes[nxt].price <= price:
                target = reach
                break
        bought = (target if target is not None else model.after_purchase_range) - fuel
        if bought <= 0:
            return None
        cost += bought * price / _TEN
        fuel = fuel + bought - legs[position]
        if fuel < 0:
            return None
    return cost


# --- Plain DP: the §15 state set without the speed-up -------------------------------------


def plain_dp_plan(instance: Instance) -> ReferencePlan | None:
    """§15 DP, every state against every edge, labels holding the full (mile, id) tuple."""
    with localcontext(_exact_context()):
        model = _Model(instance)
        if instance.route_miles <= model.first_range:
            return ReferencePlan(cost=_ZERO, sequence=())
        states: list[dict[Decimal, tuple[Decimal, int, StopSequence]]] = [{} for _ in model.nodes]
        for j in range(1, model.finish):
            if model.planned(0, j) <= model.first_range:
                states[j][model.first_range - model.planned(0, j)] = (_ZERO, 0, ())
        u = model.after_purchase_range
        for i in range(1, model.finish):
            price = model.nodes[i].price
            for fuel, (cost, stops, sequence) in states[i].items():
                for j in range(i + 1, model.finish + 1):
                    leg = model.planned(i, j)
                    if leg > u:
                        continue
                    if j == model.finish or model.nodes[j].price <= price:
                        if not fuel < leg:
                            continue
                        bought, arrival = leg - fuel, _ZERO
                    else:
                        if not fuel < u:
                            continue
                        bought, arrival = u - fuel, u - leg
                    label = (cost + bought * price / _TEN, stops + 1, (*sequence, model.key(i)))
                    if arrival not in states[j] or label < states[j][arrival]:
                        states[j][arrival] = label
        final = states[model.finish].get(_ZERO)
        return None if final is None else ReferencePlan(cost=final[0], sequence=final[2])


# --- Any-amount reference: purchases are free amounts, not the §15 rule ------------------


def any_amount_cost(instance: Instance) -> Decimal | None:
    """Cheapest cost when each stop may buy any whole number of planned miles, 0 included.

    Only for whole-mile instances (integer legs, F and U), where an integer grid of fuel
    levels is exact. Fuel after a purchase never exceeds U; fuel above U is never sold.
    """
    with localcontext(_exact_context()):
        model = _Model(instance)
        first, tank = int(model.first_range), int(model.after_purchase_range)
        if instance.route_miles <= first:
            return _ZERO
        best: list[dict[int, Decimal]] = [{} for _ in model.nodes]  # fuel on arrival -> cost
        for j in range(1, model.finish):
            leg = _whole(model.planned(0, j))
            if leg <= first:
                best[j][first - leg] = _ZERO
        finish_costs: list[Decimal] = []
        for i in range(1, model.finish):
            price = model.nodes[i].price
            # after[h] = cheapest way to leave i with h: min over g <= h of cost(g) + (h - g) p/10.
            after: dict[int, Decimal] = {g: c for g, c in best[i].items() if g > tank}
            cheapest_base: Decimal | None = None
            for level in range(tank + 1):
                if level in best[i]:
                    base = best[i][level] - level * price / _TEN
                    cheapest_base = base if cheapest_base is None else min(cheapest_base, base)
                if cheapest_base is not None:
                    after[level] = cheapest_base + level * price / _TEN
            for level, cost in after.items():
                for j in range(i + 1, model.finish + 1):
                    leg = _whole(model.planned(i, j))
                    if leg > level:
                        continue
                    arrival = level - leg
                    if j == model.finish:
                        finish_costs.append(cost)
                    elif arrival not in best[j] or cost < best[j][arrival]:
                        best[j][arrival] = cost
        return min(finish_costs) if finish_costs else None


def _whole(miles: Decimal) -> int:
    """Whole-mile instances only: a leg with a fraction means the instance is unsuitable."""
    if miles != miles.to_integral_value():
        raise ValueError(f"any-amount reference needs whole-mile legs, got {miles}")
    return int(miles)


# --- Hints and the replay checker ---------------------------------------------------------


def expected_hint(instance: Instance) -> str:
    """FR-23 hint decided by Ref A feasibility under (50, r), (current, 0) and (50, 0)."""
    fifty = Decimal("50")

    def works(start: Decimal, reserve: Decimal) -> bool:
        trial = instance.with_fuel(start_gallons=start, reserve_gallons=reserve)
        return brute_force_plan(trial) is not None

    more_fuel = instance.start_gallons < fifty and works(fifty, instance.reserve_gallons)
    lower_reserve = instance.reserve_gallons > 0 and works(instance.start_gallons, _ZERO)
    if more_fuel and lower_reserve:
        return "start with more fuel or lower reserve_gallons"
    if more_fuel:
        return "start with more fuel"
    if lower_reserve:
        return "lower reserve_gallons"
    if works(fifty, _ZERO):
        return "start with more fuel and lower reserve_gallons (both needed)"
    return "no station in the CSV covers this stretch; fuel settings cannot fix it"


def replay_violations(instance: Instance, plan: FuelPlan, exact: ExactPlan) -> list[str]:
    """Every FR-20/21/22 and I3/I4 property a final plan breaks (empty list = all hold).

    `exact` is the DP's plan: the final stops must be its stops with some removed, and each
    kept stop must buy by the rule the DP chose for it.
    """
    with localcontext(_exact_context()):
        model = _Model(instance)
        index_of = {model.nodes[k].station_id: k for k in range(1, model.finish)}
        path = [0, *(index_of[s.candidate.station_id] for s in plan.stops), model.finish]
        problems = _type_problems(plan) + _subsequence_problems(plan, exact)
        if problems:
            return problems
        rule_of = {s.candidate.station_id: s.rule for s in exact.stops}
        tank = instance.start_gallons
        for leg_number, (a, b) in enumerate(zip(path, path[1:], strict=False)):
            planned, driven = model.planned(a, b), model.driven(a, b)
            if not planned <= (tank - model.reserve) * _TEN <= (50 - model.reserve) * _TEN <= 500:
                problems.append(f"leg {leg_number}: planned {planned} > tank {tank}")
            arrival = tank - driven / _TEN
            if arrival < model.reserve + model.margin_of(a, b) / _TEN:
                problems.append(f"leg {leg_number}: arrival {arrival} below reserve + margin")
            if b == model.finish:
                if arrival != plan.finish_arrival_gallons:
                    problems.append(f"finish arrival {plan.finish_arrival_gallons} != {arrival}")
                break
            stop = plan.stops[leg_number]
            problems.extend(_stop_problems(stop, arrival=arrival, leg_number=leg_number))
            problems.extend(
                _rule_problems(
                    model,
                    stop=stop,
                    rule=rule_of[stop.candidate.station_id],
                    arrival=arrival,
                    planned_to_next=model.planned(b, path[leg_number + 2]),
                    next_is_finish=path[leg_number + 2] == model.finish,
                )
            )
            tank = arrival + stop.gallons_to_buy
        problems.extend(_total_problems(instance, model, plan))
        return problems


def _subsequence_problems(plan: FuelPlan, exact: ExactPlan) -> list[str]:
    """The replay may only drop stops: the final ids appear, in order, in the DP's ids."""
    remaining = iter(stop.candidate.station_id for stop in exact.stops)
    final_ids = [stop.candidate.station_id for stop in plan.stops]
    if all(station_id in remaining for station_id in final_ids):
        return []
    return [f"final stops {final_ids} are not the DP's stops with some removed"]


def _rule_problems(
    model: _Model,
    *,
    stop: PlannedStop,
    rule: str,
    arrival: Decimal,
    planned_to_next: Decimal,
    next_is_finish: bool,
) -> list[str]:
    """Fill buys floor(50 - a); just-enough buys min(ceil(r + planned/10 - a), floor(50 - a))."""
    room = int((50 - arrival).to_integral_value(rounding=ROUND_FLOOR))
    if rule == "fill":
        expected, label = room, "fill_tank"
    else:
        needed = model.reserve + planned_to_next / _TEN - arrival
        expected = min(int(needed.to_integral_value(rounding=ROUND_CEILING)), room)
        label = "to_finish" if next_is_finish else "to_next_stop"
    if (stop.gallons_to_buy, stop.purchase_rule) != (expected, label):
        return [
            f"station {stop.candidate.station_id}: {stop.gallons_to_buy} gal {stop.purchase_rule},"
            f" rule {rule} expects {expected} gal {label}"
        ]
    return []


def _type_problems(plan: FuelPlan) -> list[str]:
    """Money, gallons and miles must be Decimal (NFR-5); purchases must be ints."""
    problems = []
    decimals = [
        plan.total_cost,
        plan.exact_cost,
        plan.finish_arrival_gallons,
        plan.driven_distance_miles,
        plan.planning_margin_miles,
        plan.stop_margin_miles,
    ]
    for stop in plan.stops:
        decimals += [
            stop.arrival_gallons,
            stop.line_cost,
            stop.driven_miles_to_next,
            stop.planned_miles_to_next,
        ]
        if type(stop.gallons_to_buy) is not int:
            problems.append(f"gallons_to_buy {stop.gallons_to_buy!r} is not an int")
    if any(type(value) is not Decimal for value in decimals):
        problems.append("a money, gallon or mile value is not a Decimal")
    if type(plan.gallons_bought) is not int:
        problems.append("gallons_bought is not an int")
    return problems


def _stop_problems(stop: PlannedStop, *, arrival: Decimal, leg_number: int) -> list[str]:
    """One stop: whole gallons >= 1, never above 50 gal, also on the shown (0.1 down) arrival."""
    problems = []
    if stop.arrival_gallons != arrival:
        problems.append(f"stop {leg_number}: arrival {stop.arrival_gallons} != {arrival}")
    if stop.gallons_to_buy < 1:
        problems.append(f"stop {leg_number}: buys {stop.gallons_to_buy}")
    if arrival + stop.gallons_to_buy > 50:
        problems.append(f"stop {leg_number}: tank {arrival + stop.gallons_to_buy} > 50")
    shown = arrival.quantize(Decimal("0.1"), rounding=ROUND_FLOOR, context=Context(prec=50))
    if shown + stop.gallons_to_buy > Decimal("50.0"):
        problems.append(f"stop {leg_number}: shown {shown} + {stop.gallons_to_buy} > 50.0")
    if stop.line_cost != stop.gallons_to_buy * stop.candidate.price_per_gallon:
        problems.append(f"stop {leg_number}: line cost {stop.line_cost} != gallons x price")
    return problems


def _total_problems(instance: Instance, model: _Model, plan: FuelPlan) -> list[str]:
    """Totals: exact sums (I4), cost within one gallon per stop, driven = L + sum of detours."""
    problems = []
    total = sum((s.gallons_to_buy * s.candidate.price_per_gallon for s in plan.stops), _ZERO)
    if plan.total_cost != total:
        problems.append(f"total {plan.total_cost} != sum of lines {total}")
    if plan.gallons_bought != sum(s.gallons_to_buy for s in plan.stops):
        problems.append("gallons_bought != sum of purchases")
    price_sum = sum((s.candidate.price_per_gallon for s in plan.stops), _ZERO)
    if plan.total_cost - plan.exact_cost > price_sum:
        problems.append(f"rounded {plan.total_cost} - exact {plan.exact_cost} > {price_sum}")
    driven = instance.route_miles + sum((s.candidate.detour_miles for s in plan.stops), _ZERO)
    if plan.driven_distance_miles != driven:
        problems.append(f"driven {plan.driven_distance_miles} != L + detours {driven}")
    if plan.planning_margin_miles != 2 * model.margin * len(plan.stops):
        problems.append(f"planning margin {plan.planning_margin_miles} is not 2 x margin x stops")
    return problems


# --- Seeded instance generators -----------------------------------------------------------


def _hundredths(value: int) -> Decimal:
    return Decimal(value).scaleb(-2)


def _tenths(value: int) -> Decimal:
    return Decimal(value).scaleb(-1)


def _thousandths(value: int) -> Decimal:
    return Decimal(value).scaleb(-3)


def _random_price(rng: random.Random) -> Decimal:
    """60% from a small pool, so price ties are common; else a random 3-decimal price."""
    if rng.random() < 0.6:
        return Decimal(rng.choice(_PRICE_POOL))
    return _thousandths(rng.randint(2500, 4500))


def _random_detour(rng: random.Random, *, max_hundredths: int = 250) -> Decimal:
    """Estimated detours are 2 x a 0.01 offset, so even hundredths up to 5.00 mi."""
    return _hundredths(2 * rng.randint(0, max_hundredths))


def general_instance(seed: int) -> Instance:
    """TC-15 instance: n <= 8, forced price ties, same-mile pairs, mixed fuel settings."""
    rng = random.Random(seed)
    count = rng.randint(3, 8) if rng.random() < 0.9 else rng.randint(0, 1)
    reserve = rng.choice(
        [Decimal("0.0"), Decimal("5.0"), Decimal("5.0"), _tenths(rng.randint(0, 300))]
    )
    start = Decimal("50.0") if rng.random() < 0.75 else _tenths(rng.randint(int(reserve * 10), 500))
    tank_miles = (49 - reserve) * 10
    low = int(tank_miles * 120)  # hundredths of a mile: 1.2 U
    high = max(low, int(min(tank_miles * 240, tank_miles * 80 * (count + 1))))
    route_miles = _hundredths(rng.randint(low, high))
    miles: list[Decimal] = []
    walk = _ZERO
    for _ in range(count):
        if miles and rng.random() < 0.15:
            mile = miles[-1]
        elif rng.random() < 0.7:
            walk += _hundredths(rng.randint(int(tank_miles * 20), int(tank_miles * 95)))
            mile = min(walk, route_miles)
        else:
            mile = _hundredths(rng.randint(0, int(route_miles * 100)))
        miles.append(mile)
    all_verified = rng.random() < 0.15
    candidates = [
        Candidate(
            station_id=station_id,
            route_mile=mile,
            detour_miles=_random_detour(rng),
            price_per_gallon=_random_price(rng),
            is_detour_verified=all_verified or rng.random() < 0.2,
        )
        for station_id, mile in zip(rng.sample(range(1, 100_000), count), miles, strict=True)
    ]
    rng.shuffle(candidates)
    return Instance(seed, route_miles, tuple(candidates), start, reserve)


def drop_focused_instance(seed: int) -> Instance:
    """TC-18 drop set: cheap A, dearer B just under U after A and close to the finish.

    The DP fills at A and buys a sliver at B, which whole-gallon rounding often makes 0.
    """
    rng = random.Random(seed)
    reserve = rng.choice(
        [Decimal("0.0"), Decimal("5.0"), Decimal("5.0"), _tenths(rng.randint(0, 200))]
    )
    start = Decimal("50.0")
    first_range = (start - reserve) * 10
    tank_miles = (49 - reserve) * 10
    mile_a = _hundredths(rng.randint(int(first_range * 50), int((first_range - 5) * 100)))
    mile_b = mile_a + _hundredths(
        rng.randint(int((tank_miles - 60) * 100), int((tank_miles - 2) * 100))
    )
    route_miles = mile_b + _hundredths(rng.randint(1, 3000))
    price_a = _thousandths(rng.randint(2800, 3200))
    candidates = [
        Candidate(1, mile_a, _random_detour(rng, max_hundredths=50), price_a, False),
        Candidate(
            2,
            mile_b,
            _random_detour(rng, max_hundredths=50),
            price_a + _thousandths(rng.randint(1, 800)),
            False,
        ),
    ]
    for station_id in range(3, 3 + rng.randint(0, 2)):
        mile = _hundredths(rng.randint(0, int(route_miles * 100)))
        candidates.append(
            Candidate(station_id, mile, _random_detour(rng), _random_price(rng), False)
        )
    rng.shuffle(candidates)
    return Instance(seed, route_miles, tuple(candidates), start, reserve)


def plain_dp_instance(seed: int) -> Instance:
    """Plain-DP comparison instance: n 20-60 over a long route, so the speed-up really works."""
    rng = random.Random(seed)
    count = rng.randint(20, 60)
    reserve = rng.choice([Decimal("0.0"), Decimal("5.0"), _tenths(rng.randint(0, 300))])
    start = Decimal("50.0") if rng.random() < 0.6 else _tenths(rng.randint(int(reserve * 10), 500))
    tank_miles = (49 - reserve) * 10
    route_miles = _hundredths(rng.randint(int(tank_miles * 150), int(tank_miles * 500)))
    candidates = [
        Candidate(
            station_id=station_id,
            route_mile=_hundredths(rng.randint(0, int(route_miles * 100))),
            detour_miles=_random_detour(rng),
            price_per_gallon=_random_price(rng),
            is_detour_verified=False,
        )
        for station_id in rng.sample(range(1, 100_000), count)
    ]
    return Instance(seed, route_miles, tuple(candidates), start, reserve)


def whole_mile_instance(seed: int) -> Instance:
    """Any-amount instance: whole miles, even detours, reserve 38-45 so U <= 110 stays fast."""
    rng = random.Random(seed)
    reserve = Decimal(rng.choice([38, 40, 42, 45]))
    start = Decimal(rng.choice([50, 49, int(reserve) + rng.randint(0, 50 - int(reserve))]))
    tank_miles = int((49 - reserve) * 10)
    route_miles = rng.randint(tank_miles, tank_miles * 3)
    all_verified = rng.random() < 0.2
    count = rng.randint(4, 9)
    candidates = [
        Candidate(
            station_id=station_id,
            route_mile=Decimal(rng.randint(0, route_miles)),
            detour_miles=Decimal(2 * rng.randint(0, 2)),
            price_per_gallon=Decimal(rng.randint(250, 450)).scaleb(-2),
            is_detour_verified=all_verified,
        )
        for station_id in rng.sample(range(1, 1_000_000), count)
    ]
    return Instance(seed, Decimal(route_miles), tuple(candidates), start, reserve)


def perf_instance(
    *, empty_from_mile: int | None = None, empty_to_mile: int | None = None
) -> Instance:
    """TC-44: 600 candidates over 2,800 mi, prices with 5 decimals, seed 20261027.

    With an empty stretch, the 600 miles are drawn outside it, so the instance keeps 600
    candidates and the gap (560 mi > U) makes it infeasible.
    """
    rng = random.Random(PERF_SEED)
    route_hundredths = 280_000
    gap_hundredths = 0
    if empty_from_mile is not None and empty_to_mile is not None:
        gap_hundredths = (empty_to_mile - empty_from_mile) * 100
    candidates = []
    for station_id in range(1, 601):
        mile_hundredths = rng.randint(0, route_hundredths - gap_hundredths)
        if gap_hundredths and mile_hundredths >= empty_from_mile * 100:
            mile_hundredths += gap_hundredths + 1
        candidates.append(
            Candidate(
                station_id=station_id,
                route_mile=_hundredths(min(mile_hundredths, route_hundredths)),
                detour_miles=_random_detour(rng),
                price_per_gallon=Decimal(rng.randint(250_000, 450_000)).scaleb(-5),
                is_detour_verified=False,
            )
        )
    start = Decimal("50") if gap_hundredths == 0 else Decimal("45")
    return Instance(
        PERF_SEED, _hundredths(route_hundredths), tuple(candidates), start, Decimal("5")
    )
