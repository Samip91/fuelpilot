from django.contrib import admin
from django.db.models import Model
from django.http import HttpRequest

from .models import DatasetImport, Station


class ReadOnlyAdmin(admin.ModelAdmin):
    """Imported data is changed only by the import command, never by hand."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False


@admin.register(Station)
class StationAdmin(ReadOnlyAdmin):
    list_display = ["id", "name", "city", "state", "price_text", "location_quality"]
    list_filter = ["state", "location_quality"]
    search_fields = ["name", "city"]


@admin.register(DatasetImport)
class DatasetImportAdmin(ReadOnlyAdmin):
    list_display = ["dataset_version", "imported_at", "locations_source", "dev_mode"]
