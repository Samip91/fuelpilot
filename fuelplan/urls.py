from django.urls import URLPattern, URLResolver

app_name = "fuelplan"

# Routes are added with the API (spec 01 inc 2) and the map page (spec 02).
urlpatterns: list[URLPattern | URLResolver] = []
