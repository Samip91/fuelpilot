"""Startup warm-up: `planner.warm_up` loads boundary, places and stations before serving.

Every test patches fresh `LazyValue`s into the three process-wide slots, so the real
globals are never warmed and no state leaks into later tests (the FR-43 tests rely on
an unloaded station index).
"""

import sys
from collections.abc import Callable
from unittest import mock

from django.db import OperationalError, connection
from django.test.utils import CaptureQueriesContext

from fuelplan import locations, planner, stations
from fuelplan.errors import StationDataMissing
from fuelplan.locations import LazyValue, LocationDataError, get_boundary, get_gazetteer
from fuelplan.planner import warm_up
from fuelplan.stations import StationIndex, get_station_index

from .base import ApiTestCase, OfflineTestCase, assert_envelope
from .planner_harness import (
    TEST_DATASET_VERSION,
    PlannerHarness,
    json_body,
    meridian_reply,
    meridian_request,
    synthetic_index,
)

PLANNER_LOGGER = "fuelplan.planner"


class SequenceLoader:
    """A loader that raises or returns the next queued outcome on each call, and counts calls."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class CountingLoader:
    """A loader that counts its calls and returns (or raises) what it was given."""

    def __init__(self, result: object = None, *, error: Exception | None = None) -> None:
        self.result = result if result is not None else object()
        self.error = error
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def patch_lazy_values(
    test: OfflineTestCase,
    *,
    station_loader: Callable[[], object],
    gazetteer_loader: Callable[[], object],
    boundary_loader: Callable[[], object],
) -> None:
    """Swap fresh LazyValues into the three slots for the length of one test."""
    for module, name, loader in (
        (stations, "_station_index", station_loader),
        (locations, "_gazetteer", gazetteer_loader),
        (locations, "_boundary", boundary_loader),
    ):
        patcher = mock.patch.object(module, name, LazyValue(loader))
        patcher.start()
        test.addCleanup(patcher.stop)


class WarmUpLoadTests(OfflineTestCase):
    """Test 1: everything loads once and later requests reuse the same objects."""

    def test_warm_up_loads_all_three_once_and_later_getters_reuse_them(self) -> None:
        station_loader = CountingLoader(synthetic_index())
        gazetteer_loader = CountingLoader()
        boundary_loader = CountingLoader()
        patch_lazy_values(
            self,
            station_loader=station_loader,
            gazetteer_loader=gazetteer_loader,
            boundary_loader=boundary_loader,
        )

        with self.assertLogs(PLANNER_LOGGER, "INFO") as logs:
            warm_up()

        self.assertIs(get_station_index(), station_loader.result)
        self.assertIs(get_gazetteer(), gazetteer_loader.result)
        self.assertIs(get_boundary(), boundary_loader.result)
        self.assertEqual(
            (station_loader.calls, gazetteer_loader.calls, boundary_loader.calls), (1, 1, 1)
        )
        self.assertIn(
            f"Startup warm-up loaded {len(station_loader.result)} stations "
            f"(dataset {TEST_DATASET_VERSION}), places and boundary",
            logs.output[0],
        )

    def test_warm_up_closes_the_calling_threads_connections_once_after_loading(self) -> None:
        patch_lazy_values(
            self,
            station_loader=CountingLoader(synthetic_index()),
            gazetteer_loader=CountingLoader(),
            boundary_loader=CountingLoader(),
        )

        with (
            mock.patch.object(planner.connections, "close_all") as close_all,
            self.assertLogs(PLANNER_LOGGER, "INFO"),
        ):
            warm_up()

        close_all.assert_called_once_with()


class WarmUpMissingStationsTests(OfflineTestCase):
    """Tests 2-6: missing stations only warn; broken tracked files fail fast."""

    def _patch(
        self,
        *,
        station_loader: CountingLoader,
        gazetteer_loader: CountingLoader | None = None,
        boundary_loader: CountingLoader | None = None,
    ) -> tuple[CountingLoader, CountingLoader]:
        gazetteer_loader = gazetteer_loader or CountingLoader()
        boundary_loader = boundary_loader or CountingLoader()
        patch_lazy_values(
            self,
            station_loader=station_loader,
            gazetteer_loader=gazetteer_loader,
            boundary_loader=boundary_loader,
        )
        return gazetteer_loader, boundary_loader

    def test_missing_station_data_logs_a_warning_naming_the_import_and_does_not_raise(
        self,
    ) -> None:
        self._patch(station_loader=CountingLoader(error=StationDataMissing()))

        with self.assertLogs(PLANNER_LOGGER, "WARNING") as logs:
            warm_up()

        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelname, "WARNING")
        self.assertIn("StationDataMissing", logs.output[0])
        self.assertIn("import_fuel_stations", logs.output[0])

    def test_missing_station_data_still_leaves_places_and_boundary_loaded(self) -> None:
        gazetteer_loader, boundary_loader = self._patch(
            station_loader=CountingLoader(error=StationDataMissing())
        )

        with self.assertLogs(PLANNER_LOGGER, "WARNING"):
            warm_up()

        self.assertIs(get_gazetteer(), gazetteer_loader.result)
        self.assertIs(get_boundary(), boundary_loader.result)
        self.assertEqual((gazetteer_loader.calls, boundary_loader.calls), (1, 1))

    def test_after_a_failed_warm_up_the_station_index_still_raises_fr_43(self) -> None:
        station_loader = CountingLoader(error=StationDataMissing())
        self._patch(station_loader=station_loader)
        with self.assertLogs(PLANNER_LOGGER, "WARNING"):
            warm_up()

        with self.assertRaises(StationDataMissing):
            get_station_index()

        self.assertEqual(station_loader.calls, 2)

    def test_a_database_without_tables_logs_a_warning_and_does_not_raise(self) -> None:
        self._patch(station_loader=CountingLoader(error=OperationalError("no such table")))

        with self.assertLogs(PLANNER_LOGGER, "WARNING") as logs:
            warm_up()

        self.assertIn("OperationalError", logs.output[0])
        self.assertIn("import_fuel_stations", logs.output[0])

    def test_an_unreadable_places_file_propagates_so_the_server_fails_fast(self) -> None:
        self._patch(
            station_loader=CountingLoader(synthetic_index()),
            gazetteer_loader=CountingLoader(error=OSError("data/us_places.tsv unreadable")),
        )

        with self.assertRaises(OSError):
            warm_up()

    def test_an_unreadable_places_file_still_closes_the_connections_once(self) -> None:
        self._patch(
            station_loader=CountingLoader(synthetic_index()),
            gazetteer_loader=CountingLoader(error=OSError("data/us_places.tsv unreadable")),
        )

        with (
            mock.patch.object(planner.connections, "close_all") as close_all,
            self.assertRaises(OSError),
        ):
            warm_up()

        close_all.assert_called_once_with()

    def test_a_broken_boundary_file_propagates_before_any_station_load(self) -> None:
        station_loader = CountingLoader(synthetic_index())
        self._patch(
            station_loader=station_loader,
            boundary_loader=CountingLoader(error=LocationDataError("boundary GeoJSON is broken")),
        )

        with self.assertRaises(LocationDataError):
            warm_up()

        self.assertEqual(station_loader.calls, 0)


class WsgiStartupTests(OfflineTestCase):
    """Test 7: the WSGI module, which runserver and gunicorn import, runs the warm-up."""

    def test_importing_the_wsgi_module_calls_warm_up_once_and_sets_the_application(
        self,
    ) -> None:
        # patch.dict restores sys.modules afterwards, so no later test sees this import.
        with (
            mock.patch.dict(sys.modules),
            mock.patch("fuelplan.planner.warm_up") as fake_warm_up,
        ):
            sys.modules.pop("config.wsgi", None)
            import config.wsgi

        fake_warm_up.assert_called_once_with()
        self.assertTrue(callable(config.wsgi.application))


class WarmUpRealDatabaseTests(OfflineTestCase):
    """Test 8: the real loader on the empty test database."""

    def test_an_empty_station_table_logs_a_warning_with_at_most_2_select_queries(self) -> None:
        patch_lazy_values(
            self,
            station_loader=StationIndex.from_database,
            gazetteer_loader=CountingLoader(),
            boundary_loader=CountingLoader(),
        )

        with (
            CaptureQueriesContext(connection) as queries,
            self.assertLogs(PLANNER_LOGGER, "WARNING") as logs,
        ):
            warm_up()

        self.assertIn("StationDataMissing", logs.output[0])
        selects = [query for query in queries if query["sql"].lstrip().startswith("SELECT")]
        self.assertLessEqual(len(selects), 2)


class WarmUpThenRequestTests(ApiTestCase):
    """Test 9: TC-43 over HTTP through the real process-wide index after a failed warm-up."""

    def test_a_request_after_a_failed_warm_up_is_the_500_with_0_routing_calls(self) -> None:
        real_boundary = locations._boundary  # the harness resolver needs the real boundary
        patch_lazy_values(
            self,
            station_loader=StationIndex.from_database,  # the test DB is empty
            gazetteer_loader=CountingLoader(),  # the harness uses its fixture gazetteer
            boundary_loader=real_boundary.get,
        )
        with self.assertLogs(PLANNER_LOGGER, "WARNING"):
            warm_up()
        harness = PlannerHarness()
        harness.station_loader = get_station_index
        patcher = harness.patch_view()
        patcher.start()
        self.addCleanup(patcher.stop)

        response = self.post_json(meridian_request(300.0))

        assert_envelope(self, response, status=500, code="internal_error")
        self.assertIn("import_fuel_stations", json_body(response)["error"]["message"])
        self.assertEqual(harness.transport.count, 0)


class StartBeforeImportTests(ApiTestCase):
    """Owner scenario: the server starts before migrate/import; no restart is needed after."""

    def setUp(self) -> None:
        super().setUp()
        real_boundary = locations._boundary  # the harness resolver needs the real boundary
        self.index = synthetic_index()
        # Start: no tables yet; first request: tables but no rows; then imported.
        self.station_loader = SequenceLoader(
            OperationalError("no such table: fuelplan_station"), StationDataMissing(), self.index
        )
        patch_lazy_values(
            self,
            station_loader=self.station_loader,
            gazetteer_loader=CountingLoader(),  # the harness uses its fixture gazetteer
            boundary_loader=real_boundary.get,
        )
        self.harness = PlannerHarness()
        self.harness.station_loader = get_station_index
        patcher = self.harness.patch_view()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_after_migrate_and_import_the_next_request_loads_the_stations_without_a_restart(
        self,
    ) -> None:
        with self.assertLogs(PLANNER_LOGGER, "WARNING") as logs:
            warm_up()
        self.assertIn("OperationalError", logs.output[0])
        self.assertIn("python manage.py migrate", logs.output[0])
        before_import = self.post_json(meridian_request(15.0))
        self.harness.queue(meridian_reply(15.0))

        after_import = self.post_json(meridian_request(15.0))

        assert_envelope(self, before_import, status=500, code="internal_error")
        self.assertEqual(after_import.status_code, 200, after_import.content)
        self.assertEqual(json_body(after_import)["meta"]["routing_calls"], 1)
        self.assertEqual(self.harness.transport.count, 1)

    def test_once_loaded_the_index_is_kept_and_the_loader_is_not_called_again(self) -> None:
        with self.assertLogs(PLANNER_LOGGER, "WARNING"):
            warm_up()
        with self.assertRaises(StationDataMissing):
            get_station_index()

        first = get_station_index()
        later = get_station_index()

        self.assertIs(first, self.index)
        self.assertIs(later, self.index)
        self.assertEqual(self.station_loader.calls, 3)
