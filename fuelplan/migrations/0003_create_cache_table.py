"""Create the database cache table during `migrate`, so a fresh setup needs no extra command.

Plans, routes and the throttle live in Django's database cache (spec 01 §6, D9). This runs
Django's own `createcachetable` on the database being migrated; it skips a table that
already exists, so running the command by hand as well stays harmless. Reverse is a
no-op: the table holds only cache rows, which are safe to keep. The table name comes from
the live `settings.CACHES`, not from this file: if its LOCATION changes later, run
`manage.py createcachetable` once.
"""

from django.core.management import call_command
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def create_cache_table(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Create the cache table on this migration's database (not always "default")."""
    call_command("createcachetable", database=schema_editor.connection.alias, verbosity=0)


class Migration(migrations.Migration):
    dependencies = [("fuelplan", "0002_osrm_provider_slot")]

    operations = [migrations.RunPython(create_cache_table, migrations.RunPython.noop)]
