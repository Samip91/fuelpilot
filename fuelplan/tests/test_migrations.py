"""Brief review-fixes 5b: `migrate` alone creates the database cache table."""

from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from .base import OfflineTransactionTestCase

CACHE_TABLE = settings.CACHES["default"]["LOCATION"]
BEFORE_CACHE_MIGRATION = ("fuelplan", "0002_osrm_provider_slot")
CACHE_MIGRATION = ("fuelplan", "0003_create_cache_table")


def _table_names() -> list[str]:
    return connection.introspection.table_names()


def _migrate_to(target: tuple[str, str]) -> None:
    """Run the real migration executor, as `manage.py migrate fuelplan <name>` would."""
    MigrationExecutor(connection).migrate([target])


class CacheTableMigrationTests(OfflineTransactionTestCase):
    # Restores the ProviderSlot row of migration 0002 after the flush that ends the test.
    serialized_rollback = True

    def setUp(self) -> None:
        super().setUp()
        # Whatever happens below, later tests get the latest schema and the cache table.
        self.addCleanup(call_command, "createcachetable", verbosity=0)
        self.addCleanup(_migrate_to, CACHE_MIGRATION)

    def test_migrate_creates_the_cache_table_when_it_is_missing(self) -> None:
        _migrate_to(BEFORE_CACHE_MIGRATION)
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE {connection.ops.quote_name(CACHE_TABLE)}")
        self.assertNotIn(CACHE_TABLE, _table_names())

        _migrate_to(CACHE_MIGRATION)

        self.assertIn(CACHE_TABLE, _table_names())

    def test_createcachetable_after_migrate_is_still_harmless(self) -> None:
        _migrate_to(CACHE_MIGRATION)

        call_command("createcachetable", verbosity=0)

        self.assertIn(CACHE_TABLE, _table_names())

    def test_unapplying_the_migration_keeps_the_table(self) -> None:
        _migrate_to(BEFORE_CACHE_MIGRATION)

        self.assertIn(CACHE_TABLE, _table_names())
