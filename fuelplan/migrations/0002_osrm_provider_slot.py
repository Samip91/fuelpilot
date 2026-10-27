"""Create the single `osrm` slot row the routing throttle claims (spec 01 §8 #3)."""

from django.db import migrations


def create_osrm_slot(apps, schema_editor):
    ProviderSlot = apps.get_model("fuelplan", "ProviderSlot")
    ProviderSlot.objects.get_or_create(name="osrm", defaults={"next_allowed_at": 0.0})


def remove_osrm_slot(apps, schema_editor):
    apps.get_model("fuelplan", "ProviderSlot").objects.filter(name="osrm").delete()


class Migration(migrations.Migration):
    dependencies = [("fuelplan", "0001_initial")]

    operations = [migrations.RunPython(create_osrm_slot, remove_osrm_slot)]
