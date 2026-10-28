"""Database tables: imported stations, the import record, and the routing-call slot."""

from django.db import models


class LocationQuality(models.TextChoices):
    POI_MATCH = "poi_match", "Matched to the station itself"
    ADDRESS_MATCH = "address_match", "Matched to its street address"
    APPROXIMATE = "approximate", "Only the town or road was found"


class Station(models.Model):
    """One fuel station from the CSV, deduplicated by OPIS ID, with its location."""

    id = models.PositiveIntegerField(primary_key=True, help_text="OPIS Truckstop ID")
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=120)
    city = models.CharField(max_length=60)
    state = models.CharField(max_length=2)
    # Kept as the CSV text so the price is shown exactly as listed and never passes
    # through a float.
    price_text = models.CharField(max_length=16)
    price_variants = models.JSONField(default=list, blank=True)
    address_key = models.CharField(max_length=255, db_index=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    location_quality = models.CharField(max_length=16, choices=LocationQuality.choices)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.id} {self.name} ({self.city}, {self.state})"


class LocationsSource(models.TextChoices):
    GEOAPIFY = "geoapify", "Geoapify"
    PLACEHOLDER = "placeholder", "Placeholder (development only)"


class DatasetImport(models.Model):
    """Record of one `import_fuel_stations` run; its version keys the plan cache."""

    dataset_version = models.CharField(max_length=71, unique=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    locations_source = models.CharField(max_length=16, choices=LocationsSource.choices)
    dev_mode = models.BooleanField(default=False)
    counts = models.JSONField(default=dict)

    class Meta:
        ordering = ["-imported_at"]

    def __str__(self) -> str:
        return self.dataset_version


class ProviderSlot(models.Model):
    """The next moment a routing call may be sent, shared by all workers (spec 01 §8 #3)."""

    name = models.CharField(max_length=20, primary_key=True)
    next_allowed_at = models.FloatField(default=0.0, help_text="Epoch seconds")

    def __str__(self) -> str:
        return self.name
