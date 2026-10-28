# Coding Standards — FuelPilot

Short, enforced rules for this repo. Formatting, linting and import order are
checked by **ruff** (dev-only, `requirements-dev.txt`); everything else is checked
in review. A change that breaks a rule is fixed before it is committed.

## 1. Python and type hints
- Type hints on every function and method boundary (parameters and return).
  Local variables may rely on inference.
- No bare `Any`. Be specific: `Optional[X]`, `X | None`, `dict[str, Decimal]`,
  `Literal["fill_tank", "to_next_stop", "to_finish"]`.
- Keyword-only arguments (`*`) on functions with more than two parameters or any
  two of the same type: `plan_stops(*, route_miles, candidates, start_gallons, reserve_gallons)`.
- Money, gallons and miles on the money path are `Decimal`, never `float`
  (floats only for map geometry). Build `Decimal` from strings, never from floats.
- No `# type: ignore` / `# noqa` without a one-line reason on the same line.

## 2. Structure and OOP
Use a class when it holds state or groups behaviour behind one clear
responsibility; use a plain function when it is a stateless calculation.
- **Value objects:** `@dataclass(frozen=True, slots=True)` for data passed between
  layers (`Place`, `StationRecord`, `Candidate`, `Route`, `Plan`, `Stop`,
  `VerificationResult`). Immutable, typed, no behaviour beyond small derived values.
- **Service objects:** small classes that own one resource or rule —
  `RoutingClient` (OSRM calls), `CallBudget`, `ProviderThrottle`, `StationIndex`,
  `Gazetteer`, `Boundary`. Each has one job; if describing it needs "and", split it.
- **Pure algorithms stay pure:** the optimiser and geometry helpers are plain
  functions over value objects — no Django, no I/O, no globals — so they are
  testable and explainable on their own.
- **Composition over inheritance.** No deep class hierarchies; no base classes
  "for later". A `Protocol` is fine where two implementations really exist
  (the transport and its test fake; the `verify_detours` seam and its no-op).
- **Dependency injection:** pass collaborators in (transport, clock, budget,
  verifier) instead of importing globals inside functions — this is what makes
  the counting fake transport and the fake clock possible.
- **No god objects.** `planner` orchestrates; it does not compute geometry,
  prices or HTTP itself.

## 3. Layers
- **Views orchestrate only:** validate input (serializer), call the planner,
  return the response. No business logic, no `Model.objects.*` in views.
- **Services receive plain values,** never a `request`; they return value objects;
  callers serialize.
- **One module per idea** (see spec 01 §11): `locations`, `stations`, `geo`,
  `routing`, `optimizer`, `planner`, `errors`, `views`.

## 4. Database and queries
- **No N+1 queries.** Never query inside a loop over rows. Load what a request or
  command needs in one query (`.values()`/`.only()`, `select_related` /
  `prefetch_related` when relations exist) or from the in-memory index.
  The request path reads stations from the `StationIndex`, not the DB.
- **Bulk writes:** imports use `bulk_create` inside one `transaction.atomic`
  block, never `.save()` per row.
- **No blind full-row saves:** when updating one row, use `update_fields=[…]`
  or a single `QuerySet.update()`.
- **No unfiltered `.all()`** on the request path.
- A test asserts the query count of the hot paths (`assertNumQueries`).

## 5. Errors and logging
- Raise domain exceptions from `fuelplan/errors.py` (e.g. `InvalidRequest`,
  `UnsupportedLocation`, `NoFeasiblePlan`, `RoutingUnavailable`), never bare
  `ValueError`/`Exception` across a module boundary.
- Every error leaving the API uses the envelope
  `{"error": {"code", "message", "details"}}` (spec 01 §7a). No stack traces,
  keys or internal details in responses.
- No silent catches: every `except` handles, re-raises or logs with context.
  Never `except Exception: pass`.
- Log with context and lazy formatting:
  `logger.warning("OSRM timeout after %s s (budget used %s)", elapsed, budget.used)`.
  Never log secrets (the Geoapify `apiKey` is redacted).
- Fail fast on programmer errors (bad config, broken invariant); degrade
  gracefully on expected failures (provider down → 502/503/504).

## 6. Naming
- `snake_case` functions, variables, modules; `PascalCase` classes;
  `UPPER_SNAKE_CASE` module constants (`ALGORITHM_VERSION`, `PLANNING_TANK_GALLONS`).
- Booleans read as predicates: `is_verified`, `has_stops`.
- Functions are verbs: `resolve_location`, `plan_stops`, `fetch_route`.
- Units in names where they matter: `distance_miles`, `timeout_s`, `price_per_gallon`.
- No abbreviations except `id`, `qs`, `lat`, `lon`.

## 7. Comments and docs
- Comments explain **why**, not what.
- Every public function and class has a one-line docstring: purpose and any
  non-obvious constraint (e.g. "raises before the socket opens").
- No commented-out code. No bare `TODO` — write `TODO(spec-03):` or remove it.
- No stale comments.

## 8. Tests
- Django test runner (`python manage.py test`); no network (sockets blocked in the
  base test case), no real clock (inject a fake clock).
- Names read as sentences: `test_cold_request_makes_exactly_one_routing_call`.
- Arrange–Act–Assert; one behaviour per test.
- Test behaviour and contracts (status, body, call count, query count), not
  private implementation details.
- Every spec TC maps to a named test (spec §10 "Evidence").

## 9. Git
- **Branches:** `main` holds releases only; `develop` is the integration branch;
  each build step is a `feature/<spec>-<name>` branch cut from `develop`
  (`chore/…`, `docs/…`, `fix/…` for other work).
- **Pull requests:** every branch reaches `develop` through a GitHub pull request,
  never a direct push or local merge; `develop` reaches `main` through a release PR.
- Conventional commits with a scope, imperative mood: `feat(import): add station import command`.
- One logical change per commit.
- No `print()`, debug logging or skipped tests in commits.
- Commit messages, PR titles and PR descriptions never mention AI tools; the owner
  approves each one before it is used.

## Enforcement
| Area | How |
|---|---|
| Formatting, lint, import order | `ruff format` + `ruff check` (config in `ruff.toml`) |
| Tests | `python manage.py test` must pass before every commit |
| Query counts | `assertNumQueries` on the hot paths |
| Everything else | review of every diff against this file and the specs |
