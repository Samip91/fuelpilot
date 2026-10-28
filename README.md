# FuelPilot

A Django API for truck trips inside the USA. You send a start and a finish (a city or
coordinates). FuelPilot returns the road route as a map, the cheapest fuel stops for a
vehicle with a 500-mile range, and the total fuel cost at 10 miles per gallon. Every stop
and every price comes from the provided fuel-price CSV.

Loom walkthrough: (link added after recording)

## Quick start

You need **Python 3.12 or newer** (it installs Django 6.1.2, DRF 3.18.3 and python-dotenv
1.2.4), git and internet (for the routing service and the map tiles). **No API key needed.**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Make a secret key, then paste it after DJANGO_SECRET_KEY= in .env (the only value to set):
python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
python manage.py migrate
python manage.py createcachetable
python manage.py import_fuel_stations
python manage.py runserver
```

`import_fuel_stations` needs no key and no network. Its summary: 8,151 rows read, 0 invalid,
620 non-US rows dropped, 867 duplicate IDs merged, 219 unresolved, 0 missing a location,
**6,445 stations imported**, dataset `sha256:965a7240…`. Restart the server after a re-import.

## Your first request

```bash
curl -s -X POST http://localhost:8000/api/v1/fuel-plans \
  -H "Content-Type: application/json" \
  -d '{"start": {"city": "Chicago", "state": "IL"}, "finish": {"city": "Denver", "state": "CO"}}'
```

Each endpoint is **either** a city and state **or** coordinates (JSON numbers). Optional, at
most one decimal: `current_fuel_gallons` (default 50, 0–50), `reserve_gallons` (default 5, 0–48).

```json
{"start": {"latitude": 32.7767, "longitude": -96.797}, "finish": {"latitude": 29.7604,
 "longitude": -95.3698}, "current_fuel_gallons": 50, "reserve_gallons": 5}
```

### The response

A real first response from the final data; cuts are marked `/* … */`:

```jsonc
{
  "plan_id": "8d829f1a-3354-41d1-91b3-9ce637d22e78",
  "map_url": "http://localhost:8000/api/v1/fuel-plans/8d829f1a-3354-41d1-91b3-9ce637d22e78/map",
  "summary": {
    "headline": "Chicago, IL → Denver, CO: 1004.8 mi, 2 fuel stops, total fuel cost $163.18 for 56 gal. Each stop's cost is rounded to the cent and the total is rounded once, so they can differ by a cent or two.",
    "stops": [
      "1. Mile 322.8 — KWIK STAR #932, Altoona, IA (I-80, EXIT 143 & CR-S14): arrive with 17.5 gal, buy 14 gal × $2.959 = $41.43 — just enough to reach the next, cheaper stop. Town-level location: check the exact exit before you go.",
      "2. Mile 578.1 — Henderson Fuel Mart, Henderson, NE (I-80, EXIT 342): arrive with 5.9 gal, buy 42 gal × $2.899 = $121.76 — enough to reach the finish."
    ],
    "finish": "Arrive in Denver, CO with 5.2 gal left. The start tank (50.0 gal) is not charged."
  },
  "start": {"input": {"city": "Chicago", "state": "IL"}, "matched": "Chicago, IL", /* "location" */},
  "finish": {/* same shape: "matched": "Denver, CO" */},
  "fuel_stops": [
    {"sequence": 1, "station_id": "70333", "name": "KWIK STAR #932", "address": "I-80, EXIT 143 & CR-S14",
     "city": "Altoona", "state": "IA", "location": {"type": "Point", "coordinates": [-93.4696, 41.64373]},
     "route_mile": "322.8", "estimated_detour_miles": "2.8", "fuel_on_arrival_gallons": "17.5",
     "purchase_rule": "to_next_stop", "gallons_to_buy": 14, "price_per_gallon": "2.959", "line_cost": "41.43",
     "miles_to_next": "256.8", "location_quality": "approximate", "detour_verified": false},
    {/* stop 2, same fields: Henderson Fuel Mart, 42 gal × 2.899 = "121.76", "poi_match" */}
  ],
  "finish_fuel_on_arrival_gallons": "5.2",
  "totals": {"currency": "USD", "gallons_bought": 56, "fuel_burned_gallons": "100.8", "total_purchase_cost": "163.18"},
  "assumptions": {"mpg": "10", "tank_gallons": "50", "planning_tank_gallons": "49", /* 13 more */},
  "detours_verified": false, "notes": [/* 7 notes */], "warnings": [/* approximate_location for 70333 */],
  "meta": {"routing_calls": 1, "route_cache": "miss", "plan_cache": "miss"},
  "route": {"distance_miles": "1004.8", "driven_distance_miles": "1007.6", "planning_margin_miles": "4.0",
            "geometry": {"type": "LineString", "coordinates": [[-87.6501, 41.85003], /* 595 more, 596 points in total */]}}
}
```

Sending it again within 24 hours returns the same `plan_id` with `"routing_calls": 0`. From top to bottom:

- `plan_id`, `map_url` — the saved plan and its map page (open it in a browser).
- `summary` — headline, one sentence per stop, finish. **Display text only:** read `fuel_stops`.
- `start`, `finish` — your input, `matched` (e.g. "Chicago, IL"; `null` for coordinates), the point used.
- `fuel_stops` — each stop in route order: station, route mile, estimated detour, fuel on
  arrival, `gallons_to_buy`, the CSV price, `line_cost`, `location_quality`.
- `location_quality`: `poi_match` | `address_match` | `approximate`; `purchase_rule`: `fill_tank` | `to_next_stop` | `to_finish`; `warnings[].code`: `approximate_location` | `far_snap`; `meta.route_cache`: `hit` | `miss` | `skipped`; `meta.plan_cache`: `hit` | `miss`.
- `finish_fuel_on_arrival_gallons`, `totals` — gallons bought, fuel burned, total cost.
- `assumptions` — every planning constant, so the numbers can be checked by hand.
- `detours_verified`, `notes`, `warnings` — what is estimated, and stops to double-check.
- `meta` — `routing_calls`, `route_cache`, `plan_cache` for this response.
- `route` — `distance_miles` is from the router, `driven_distance_miles` adds the estimated detours,
  `planning_margin_miles` is 1 mi per stop end; `geometry` is the road line for the map: GeoJSON
  `[lon, lat]`, under 2,000 points. It comes last because it is long.

Money, miles and gallons are strings (`"183.04"`); `gallons_to_buy` and
`totals.gallons_bought` are integers. No CORS headers: call it from your server or the same origin.

## Postman

Import [postman/fuelpilot.postman_collection.json](postman/fuelpilot.postman_collection.json) (variable `base_url`, default
`http://localhost:8000`). Its 9 requests have tests: a long trip, the same trip again (0
calls, same `plan_id`), coordinates, outside the USA, no feasible plan, the map page, an
invalid body, an unknown plan, a low-fuel start. One run sends 7 POSTs; the 30/min limit allows about 4 runs a minute.

## How it works

1. Validate the body; resolve cities offline from a GeoNames file (0 calls); reject
   points outside the lower 48 + DC.
2. Get the road route: one call to the OSRM demo server, or 0 from the 24-hour route cache.
3. Find the stations in a corridor along the route (estimated detour ≤ 5 mi).
4. Optimise: an exact dynamic program over the stops in route order chooses where to fill up
   and where to buy just enough. Checked against a brute-force reference (n ≤ 8), a plain DP
   and an any-amount optimum (`test_dp_matches_brute_force_on_cost_sequence_and_feasibility`,
   `test_sped_up_dp_matches_plain_dp_on_100_larger_instances`, `test_dp_cost_and_feasibility_match_the_any_amount_optimum`).
5. Replay the plan in whole gallons and check every leg against the tank.
6. Save the plan for 7 days, build the response and the map link.

## Assumptions in plain words

- **Range.** 50 gal × 10 MPG = 500 mi. With the default 5-gal reserve (kept in the tank at every
  stop and at the finish) the first leg can be 450 mi. After a purchase the planner uses a 49-gal
  tank (room for whole-gallon rounding), so 440 mi. With reserve 0: 500 / 490 mi. Each stop end
  also keeps a 1-mi margin for the estimated detour. No leg is ever longer than 500 mi.
- **Start tank.** The fuel you start with is not charged, so a short trip costs $0.00.
- **Cheapest** means the lowest total purchase cost; ties go to fewer stops. Stations at the
  same route mile are tried in station-ID order; another order rarely saves a cent.
- **Many small stops.** A stop has no cost of its own and there is no minimum purchase, so long
  trips can add short top-ups (1–8 gal) at cheaper stations: Hartford → Los Angeles has 9 stops and
  is $134 cheaper than filling up at the last station in range.
- **Whole gallons.** "Fill" stops round down, other stops round up just enough.
- **Rounding.** Each line cost is rounded to the cent; the total is the unrounded sum, rounded once
  (the response above shows it too: 41.43 + 121.76 = 163.19, total 163.18). Illustrative arithmetic:
  41 gal × $3.199 = 131.159 → 131.16 and 15 gal × $3.459 = 51.885 → 51.89. The lines add up to
  183.05, but the total is 183.044 → **183.04**.
- **Prices** are the CSV snapshot, not live. A station listed more than once uses its
  lowest price among rows with a known position.
- **Area.** Lower 48 states + DC only; anything else is a clear 422.
- **No partial plans.** If a stretch has no reachable station, the answer is 422
  `no_feasible_plan` with the gap and a hint, never half a plan.

## About station positions

The CSV has no coordinates. Over 95% of its addresses are highway or exit text (for example
"I-80, EXIT 46"), which geocoders can rarely place exactly. Every address was geocoded
once with Geoapify while preparing the data; the result is committed as
`data/station_locations.csv`, so running the app needs no Geoapify key and never geocodes. Most stations land on their town centre and are marked
`approximate` (4,357 of 6,445 stations, 67.6%); 1,668 (25.9%) match a street address
and 420 (6.5%) the station itself. 219 CSV rows could not be placed (100 addresses had no
result; the others landed outside the station's state or the lower 48) and are left out.

A station is a candidate when its estimated detour (leave the route, reach the station,
come back) is ≤ 5 mi, which means it lies within about 2.5 mi of the route. The detour is
twice the straight-line distance: a small estimate that both filters stations and adds
fuel to the plan, plus a 1-mi margin per stop end. It is not measured on roads.

## Routing call budget

A new request makes **1** routing call, a repeat makes **0**, and city lookups make **0**
(offline). Nothing is retried. `meta.routing_calls` shows the count. Map tiles are loaded
by your browser, not by the API. A **502, 503 or 504** with `"retryable": true` means the
OSRM demo server failed or was busy, not our code; try again shortly.
`FUELPILOT_ROUTING_BASE_URL` can point to another OSRM server (untested).

## Errors

Every error has the shape `{"error": {"code", "message", "details"}}`.

| code | HTTP | when | retry? |
|---|---|---|---|
| `invalid_request` | 400 | bad JSON, schema, type, range or precision; unknown keys; bad state code; current < reserve; body > 10 KB or no Content-Length; disallowed Host | no |
| `not_found` | 404 | unknown path | no |
| `plan_not_found` | 404 | unknown or expired plan id on the map link | no |
| `method_not_allowed` | 405 | not POST on `/fuel-plans` (see `Allow`) | no |
| `unsupported_media_type` | 415 | not `application/json` | no |
| `location_not_found` | 422 | no populated place for that city and state | no |
| `unsupported_location` | 422 | state, province or point outside lower 48 + DC | no |
| `no_route` | 422 | the routing provider found no road route; a point no road can be matched to (NoSegment) | no |
| `no_feasible_plan` | 422 | a stretch no station can cover with these fuel settings | no |
| `too_many_requests` | 429 | our limit, 30 POSTs per minute per IP | yes, `Retry-After` |
| `internal_error` | 500 | unexpected error; OSRM Invalid*/TooBig; call budget exceeded; station data not imported | no |
| `routing_unavailable` | 502 | routing provider error or bad answer; connection, DNS or TLS error, or an oversized body | yes |
| `routing_rate_limited` | 503 | routing provider busy or rate-limited | yes, `Retry-After` |
| `routing_timeout` | 504 | routing provider too slow | yes |

```json
{"error": {"code": "unsupported_location",
           "message": "The start location is outside the service area: contiguous US + DC only.",
           "details": {"field": "start", "scope": "contiguous US + DC"}}}
```

## Speed (measured)

`curl` on `runserver --noreload`, Chicago → Denver unless noted, cold times include the OSRM
demo server. "As above": Intel i7-9750HF laptop, Python 3.12.3, 2026-10-28, `sha256:965a7240…`.

| What | Result | Runs, statistic | Machine, date, dataset |
|---|---|---|---|
| First request per process (plan cached, 0 calls; loads stations and places) | 1.63 s; memory +152 MiB (RSS) | 3 restarts, median (1.60–1.68 s) | as above |
| Cold request (1 routing call) | 1.63 s | 5, median (1.56–3.08 s) | as above |
| Cache hit (0 calls) | 10 ms | 20, median (max 14 ms) | as above |
| Map page (21,615 B) | 3.6 ms | 20, median (max 7.8 ms) | as above |
| Local work, New York → Los Angeles (route cached, 0 calls) | 179 ms | 5, median (175–181 ms) | as above |
| Optimiser (synthetic 600-candidate instance, feasible plan) | 276 ms | 5, median (260–282 ms) | as above |

## Tests

Run `FUELPILOT_DEBUG=true python manage.py test`; the tests never touch the network. Lint: `pip install -r requirements-dev.txt`, then `ruff format --check . && ruff check .`.

## Good to know

- **Map links** last 7 days, then 404 `plan_not_found`; they show the plan as saved, with the
  labels of its first request. Open `map_url` in a browser (Postman's preview may not run
  JavaScript); Leaflet and the tiles need internet, and all text shows without them (checked in a browser 2026-10-28).
- **Caching.** Plans and routes live in the database cache, so a repeat within 24 h makes 0
  calls (map links last 7 days). A re-import changes the dataset version; restart after it.
- **DEBUG.** With `FUELPILOT_DEBUG=true`, non-API URLs and a disallowed Host show Django's HTML
  pages; `/api/v1/` answers JSON except for a disallowed Host. Only POSTs count toward 30/min.
- **Borders and coasts.** Points within about 2 km of a border or coast may be misjudged,
  and about 0.2% of coastal places fall outside the boundary outline. US-to-US routes that
  pass through Canada use no Canadian stations.
- **Wording.** A `no_feasible_plan` message that says "from the start" also covers a station right
  at the start. A start or finish over 1 mi from a road gets a `far_snap` warning; the plan runs.

## Data sources & licences

| File | Source and how it was made | Licence |
|---|---|---|
| `data/raw/fuel-prices.csv` | Provided for this assessment, unchanged | as provided |
| `data/station_locations.csv` | Each CSV address geocoded once with Geoapify (`geocode_stations`, owner only, not needed to run). Rows keep their own `attribution`, `license` and `source_url` | per row: ODbL / © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), OpenAddresses (BSD-3), Who's On First (CC0), GeoNames (CC BY 4.0); rows keep their source's attribution where Geoapify returned one (Geoapify allows storing results on that condition); 130 placed rows came back without a data source and are covered by the file-level ODbL notice and the Geoapify credit; as most rows are OpenStreetMap data, the file is shared under the [ODbL](https://opendatacommons.org/licenses/odbl/1-0/). [Powered by Geoapify](https://www.geoapify.com/) |
| `data/us_places.tsv` | [GeoNames](https://www.geonames.org/) US file, trimmed to populated places and 6 columns (`build_location_data --geonames`) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| `data/us_lower48_dc.geojson` | U.S. Census Bureau cartographic boundary file (`cb_2025_us_state_5m`), KML converted to GeoJSON, lower 48 + DC kept (`build_location_data --census-kml`) | Source: U.S. Census Bureau |

Services: routing by the [OSRM demo server](https://github.com/Project-OSRM/osrm-backend/wiki/Demo-server)
(non-commercial, at most 1 request per second, no SLA; FuelPilot paces its calls), road
data © OpenStreetMap contributors (ODbL). Map tiles from OpenStreetMap under the
[tile usage policy](https://operations.osmfoundation.org/policies/tiles/), with visible
attribution and no prefetching. The map uses [Leaflet](https://leafletjs.com/) 1.9.4 (BSD-2-Clause).

## Future work

- Better station positions (snap town-level stations onto the highway in their address),
  then detours measured on roads instead of estimated.
- Live fuel prices; vehicle profiles (tank size, MPG); a per-stop cost or minimum purchase; every order of same-mile stations.
- An OpenAPI description; production hosting; a Content Security Policy and a bundled Leaflet.
