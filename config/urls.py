from django.contrib import admin
from django.urls import include, path

from fuelplan.views import api_not_found

urlpatterns = [
    path("admin/", admin.site.urls),
    # "/api/v1" itself: the JSON 404, not an APPEND_SLASH redirect that drops a POST body.
    path("api/v1", api_not_found),
    path("api/v1/", include("fuelplan.urls")),
]

# JSON error envelopes outside DRF views (§7a, FR-34); Django uses them when DEBUG is off.
handler400 = "fuelplan.errors.bad_request"
handler404 = "fuelplan.errors.page_not_found"
handler500 = "fuelplan.errors.server_error"
