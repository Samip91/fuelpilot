from django.urls import URLPattern, URLResolver, path, re_path

from .views import FuelPlanView, api_not_found, fuel_plan_map

app_name = "fuelplan"

urlpatterns: list[URLPattern | URLResolver] = [
    path("fuel-plans", FuelPlanView.as_view(), name="fuel-plans"),
    # Before the catch-all; a plan id that is not a UUID falls through to the JSON 404.
    path("fuel-plans/<uuid:plan_id>/map", fuel_plan_map, name="fuel-plan-map"),
    # Keep this last: it answers every other /api/v1/ path (incl. "fuel-plans/") with
    # the JSON 404, so the API never shows Django's HTML 404 page, even with DEBUG on.
    # A bare "^" (no "$") is searched, so it matches any rest of the path.
    re_path(r"^", api_not_found, name="api-not-found"),
]
