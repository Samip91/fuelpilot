"""Spec 01 TC-13 (fetch part): owner-only batch geocoding with a fake Geoapify."""

import csv
import fcntl
import json
import shutil
import tempfile
import traceback
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from fuelplan.fuel_csv import EXPECTED_HEADER
from fuelplan.http import HttpResponse, TransportError, redact_url
from fuelplan.management.commands import geocode_stations
from fuelplan.management.commands.geocode_stations import (
    OUTPUT_COLUMNS,
    is_highway_style,
    pick_sample,
    select_todo,
)

from .base import FakeTransport, OfflineTestCase

API_KEY = "test-key-123"
ROWS = [
    ["1", "PILOT #1", "I-80, EXIT 46", "Walnut", "IA", "1", "3.199"],
    ["2", "PILOT TRAVEL CENTER #1", "I-80,  EXIT 46", "walnut", "IA", "1", "3.299"],  # same address
    ["3", "LOVES #7", "123 MAIN ST", "Sterling", "CO", "1", "3.459"],
    ["4", "ESSO", "1 KING ST", "Toronto", "ON", "1", "1.500"],  # outside the service area
    ["5", "BAD PRICE", "9 ELM ST", "Omaha", "NE", "1", "abc"],  # invalid row
]
WALNUT = "I-80, EXIT 46, Walnut, IA, United States"
STERLING = "123 MAIN ST, Sterling, CO, United States"
WALNUT_KEY = "I-80, EXIT 46|WALNUT|IA"
STERLING_KEY = "123 MAIN ST|STERLING|CO"
ATTRIBUTION = "© OpenStreetMap contributors"
LICENSE = "Open Database License"
SOURCE_URL = "https://www.openstreetmap.org/copyright"


def _item(text: str, *, lat: float, lon: float, result_type: str, state: str) -> dict:
    return {
        "query": {"text": text},
        "lat": lat,
        "lon": lon,
        "result_type": result_type,
        "rank": {"confidence": 0.9},
        "state_code": state,
        "datasource": {"attribution": ATTRIBUTION, "license": LICENSE, "url": SOURCE_URL},
    }


WALNUT_ITEM = _item(WALNUT, lat=41.48, lon=-95.22, result_type="amenity", state="ia")
STERLING_ITEM = _item(STERLING, lat=40.62, lon=-103.2, result_type="building", state="co")
WALNUT_ROW = {
    "address_key": WALNUT_KEY,
    "lat": "41.48",
    "lon": "-95.22",
    "result_type": "amenity",
    "confidence": "0.9",
    "state_code": "IA",
    "source": "geoapify",
    "attribution": ATTRIBUTION,
    "license": LICENSE,
    "source_url": SOURCE_URL,
}
STERLING_ROW = {
    "address_key": STERLING_KEY,
    "lat": "40.62",
    "lon": "-103.2",
    "result_type": "building",
    "confidence": "0.9",
    "state_code": "CO",
    "source": "geoapify",
    "attribution": ATTRIBUTION,
    "license": LICENSE,
    "source_url": SOURCE_URL,
}


@dataclass
class SnapshotTransport(FakeTransport):
    """Records the pending-jobs file as it is when each result request is sent."""

    jobs_path: Path | None = None
    snapshots: list[object] = field(default_factory=list)

    def request(
        self, *, method: str, url: str, body: bytes | None = None, **extra: object
    ) -> HttpResponse:
        if method == "GET" and self.jobs_path is not None:
            self.snapshots.append(json.loads(self.jobs_path.read_text(encoding="utf-8")))
        return super().request(method=method, url=url, body=body, **extra)


class LeakyTransport(FakeTransport):
    """Replays its queue, then fails with the raw keyed URL in the error message."""

    def request(
        self, *, method: str, url: str, body: bytes | None = None, **extra: object
    ) -> HttpResponse:
        if self.responses:
            return super().request(method=method, url=url, body=body, **extra)
        raise TransportError(f"failed calling {url}")


def _queue_job(transport: FakeTransport, items: list[dict]) -> None:
    transport.queue_json(202, {"id": "job-1", "status": "pending"})
    transport.queue_json(200, items)


@override_settings(
    GEOAPIFY_API_KEY=API_KEY,
    GEOAPIFY_BASE_URL="https://api.geoapify.com",
    GEOAPIFY_CONNECT_TIMEOUT_S=10,
    GEOAPIFY_TOTAL_TIMEOUT_S=30,
)
class GeocodeStationsCommandTests(OfflineTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.workdir)
        self.csv_path = self.workdir / "fuel.csv"
        self._write_csv(EXPECTED_HEADER)
        self.out_path = self.workdir / "locations.csv"
        patcher = mock.patch.object(geocode_stations, "DEFAULT_CSV", self.csv_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_csv(self, header: list[str]) -> None:
        with self.csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(ROWS)

    @property
    def jobs_path(self) -> Path:
        return self.workdir / "locations.csv.jobs.json"

    def _run(
        self,
        transport: FakeTransport,
        *args: str,
        sleeps: list[float] | None = None,
        max_polls: int = 3,
    ) -> str:
        output = StringIO()
        call_command(
            "geocode_stations",
            "--out",
            str(self.out_path),
            *args,
            transport=transport,
            client_options={
                "sleep": (sleeps if sleeps is not None else []).append,
                "max_polls": max_polls,
            },
            stdout=output,
        )
        return output.getvalue()

    def _write_jobs(self, jobs: object) -> None:
        self.jobs_path.write_text(json.dumps(jobs), encoding="utf-8")

    def _write_output(self, header: list[str], *rows: list[str]) -> None:
        with self.out_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)

    def _saved_rows(self) -> list[dict[str, str]]:
        with self.out_path.open(newline="", encoding="utf-8") as handle:
            return sorted(csv.DictReader(handle), key=lambda row: row["address_key"])

    def test_each_unique_service_area_address_is_sent_once(self) -> None:
        transport = FakeTransport()
        _queue_job(transport, [WALNUT_ITEM, STERLING_ITEM])

        self._run(transport)

        self.assertEqual(sorted(transport.sent_texts(0)), sorted([WALNUT, STERLING]))
        query = parse_qs(urlsplit(transport.requests[0].url).query)
        self.assertEqual(query["filter"], ["countrycode:us"])
        self.assertEqual(self._saved_rows(), [STERLING_ROW, WALNUT_ROW])

    @override_settings(
        GEOAPIFY_BASE_URL="http://geo.test:8080",
        GEOAPIFY_CONNECT_TIMEOUT_S=4,
        GEOAPIFY_TOTAL_TIMEOUT_S=45,
    )
    def test_the_command_calls_the_configured_geoapify_url_with_its_timeouts(self) -> None:
        transport = FakeTransport()
        _queue_job(transport, [WALNUT_ITEM, STERLING_ITEM])

        self._run(transport)

        sent = [
            (urlsplit(request.url).netloc, request.connect_timeout_s, request.total_timeout_s)
            for request in transport.requests
        ]
        self.assertEqual(sent, [("geo.test:8080", 4, 45)] * 2)

    @override_settings(GEOAPIFY_BASE_URL="ftp://api.geoapify.com")
    def test_a_bad_geoapify_base_url_stops_the_command_before_any_request(self) -> None:
        transport = FakeTransport()

        with self.assertRaisesMessage(CommandError, "GEOAPIFY_BASE_URL must be an http(s)"):
            self._run(transport)

        self.assertEqual(transport.count, 0)

    def test_an_address_without_a_result_is_saved_empty_not_as_a_city_centre(self) -> None:
        transport = FakeTransport()
        _queue_job(transport, [{"query": {"text": WALNUT}}, {"query": {"text": STERLING}}])

        self._run(transport)

        rows = self._saved_rows()
        self.assertEqual([row["address_key"] for row in rows], [STERLING_KEY, WALNUT_KEY])
        self.assertTrue(all(row["lat"] == "" and row["lon"] == "" for row in rows))

    def test_an_address_missing_from_the_reply_is_not_saved_so_resume_retries_it(self) -> None:
        transport = FakeTransport()
        _queue_job(transport, [WALNUT_ITEM])

        message = self._run(transport)

        self.assertEqual(self._saved_rows(), [WALNUT_ROW])
        self.assertIn("unmatched 1", message)

    def test_a_reply_that_matches_no_sent_address_fails_and_saves_nothing(self) -> None:
        transport = FakeTransport()
        stranger = _item(
            "1 OTHER RD, Nowhere, KS", lat=1, lon=2, result_type="building", state="ks"
        )
        _queue_job(transport, [stranger])

        with self.assertRaisesMessage(CommandError, "no result in the reply matches"):
            self._run(transport)

        self.assertFalse(self.out_path.exists())

    def test_resume_sends_no_request_for_addresses_already_saved(self) -> None:
        first = FakeTransport()
        _queue_job(first, [WALNUT_ITEM, STERLING_ITEM])
        self._run(first)
        second = FakeTransport()

        message = self._run(second, "--resume")

        self.assertEqual(second.count, 0)
        self.assertIn("Nothing to geocode", message)

    def test_partial_resume_sends_only_the_address_not_yet_saved(self) -> None:
        first = FakeTransport()
        _queue_job(first, [WALNUT_ITEM])
        self._run(first)
        second = FakeTransport()
        _queue_job(second, [STERLING_ITEM])

        self._run(second, "--resume")

        self.assertEqual(second.sent_texts(0), [STERLING])
        self.assertEqual(self._saved_rows(), [STERLING_ROW, WALNUT_ROW])
        header_lines = self.out_path.read_text(encoding="utf-8").count("address_key")
        self.assertEqual(header_lines, 1)

    def test_a_failed_second_batch_keeps_the_first_and_resume_sends_only_the_rest(self) -> None:
        self._use_batch_size(1)
        failing = FakeTransport()
        _queue_job(failing, [STERLING_ITEM])  # batch 1: keys sort Sterling first
        failing.queue_json(500, {"error": "boom"})  # batch 2 job creation

        with self.assertRaisesMessage(CommandError, "check the Geoapify dashboard"):
            self._run(failing)
        self.assertEqual(self._saved_rows(), [STERLING_ROW])

        retry = FakeTransport()
        _queue_job(retry, [WALNUT_ITEM])
        self._run(retry, "--resume")

        self.assertEqual(retry.sent_texts(0), [WALNUT])
        self.assertEqual(self._saved_rows(), [STERLING_ROW, WALNUT_ROW])

    def test_an_existing_output_is_never_overwritten_without_resume(self) -> None:
        self.out_path.write_text("address_key\n", encoding="utf-8")

        with self.assertRaisesMessage(CommandError, "--resume"):
            self._run(FakeTransport())

    def test_a_sample_below_one_is_rejected_before_any_request(self) -> None:
        for value in ("0", "-3", "abc"):
            transport = FakeTransport()
            with self.subTest(sample=value), self.assertRaisesMessage(CommandError, "at least 1"):
                self._run(transport, "--sample", value)
            self.assertEqual(transport.count, 0)

    @override_settings(GEOAPIFY_API_KEY="")
    def test_a_missing_api_key_stops_the_command_before_any_request(self) -> None:
        transport = FakeTransport()

        with self.assertRaisesMessage(CommandError, "GEOAPIFY_API_KEY is not set"):
            self._run(transport)

        self.assertEqual(transport.count, 0)

    def test_a_csv_with_the_wrong_header_stops_the_command(self) -> None:
        self._write_csv(["id", "name", "address", "city", "state", "rack", "price"])

        with self.assertRaisesMessage(CommandError, "unexpected header"):
            self._run(FakeTransport())

    def test_the_api_key_never_appears_in_errors_tracebacks_or_logs(self) -> None:
        transport = LeakyTransport()
        transport.queue_json(202, {"id": "job-1"})
        transport.queue_json(202, {"id": "job-1", "status": "pending"})  # poll 1 logs a line

        with (
            self.assertLogs("fuelplan", level="INFO") as logs,
            self.assertRaises(CommandError) as raised,
        ):
            self._run(transport)

        shown = "".join(traceback.format_exception(raised.exception))
        self.assertIn("apiKey=***", shown)
        self.assertNotIn(API_KEY, shown)
        self.assertNotIn(API_KEY, "\n".join(logs.output))

    # Item 1: credit budget.
    def test_a_job_that_would_pass_the_credit_budget_is_never_submitted(self) -> None:
        transport = FakeTransport()

        message = self._run(transport, "--max-credits", "7")  # 2 addresses need about 8

        self.assertEqual(transport.count, 0)
        self.assertFalse(self.out_path.exists())
        self.assertIn("Credit budget reached", message)
        self.assertIn("Addresses left: 2; rerun with --resume tomorrow.", message)

    def test_the_run_stops_before_the_batch_that_would_pass_the_budget_and_keeps_results(
        self,
    ) -> None:
        self._use_batch_size(1)
        transport = FakeTransport()
        _queue_job(transport, [STERLING_ITEM])  # 7.5 estimated, 2.5 used (0.5 + 1 + 1 poll)

        message = self._run(transport, "--max-credits", "9")  # 2.5 + 7.5 > 9: no 2nd job

        self.assertEqual(transport.count, 2)
        self.assertEqual(self._saved_rows(), [STERLING_ROW])
        self.assertIn("Addresses left: 1; rerun with --resume tomorrow.", message)
        self.assertIn("Estimated credits used: 2.5 of 9", message)

    def test_the_summary_prints_the_estimated_credits_used(self) -> None:
        transport = FakeTransport()
        transport.queue_json(202, {"id": "job-1"})
        transport.queue_json(202, {"id": "job-1", "status": "pending"})
        transport.queue_json(200, [WALNUT_ITEM, STERLING_ITEM])

        with self.assertLogs("fuelplan", level="INFO"):
            message = self._run(transport)

        # 2 addresses x 0.5 + 1 job + 2 result requests (each poll counted).
        self.assertIn("Estimated credits used: 4.0 of 2800 (1 credit per poll assumed)", message)

    def test_a_budget_that_runs_out_while_polling_keeps_the_job_for_a_later_resume(
        self,
    ) -> None:
        # 2 addresses: the submit estimate is 8 (1 + 2 + 5 reserve), so --max-credits 8
        # allows the job; it costs 2, leaving room for exactly 6 result requests.
        polling = FakeTransport()
        polling.queue_json(202, {"id": "job-1"})
        for _poll in range(6):
            polling.queue_json(202, {"id": "job-1", "status": "pending"})

        with self.assertLogs("fuelplan", level="INFO"):
            message = self._run(polling, "--max-credits", "8", max_polls=60)

        # FakeTransport fails on any request beyond its queue, so 7 is a hard ceiling.
        self.assertEqual(polling.count, 1 + 6)
        self.assertIn("Credit budget reached while waiting for job job-1", message)
        self.assertIn("Addresses left: 2; rerun with --resume tomorrow.", message)
        self.assertIn("Estimated credits used: 8.0 of 8", message)
        self.assertEqual(self._jobs(), {"job-1": [STERLING_KEY, WALNUT_KEY]})
        self.assertFalse(self.out_path.exists())

        resumed = FakeTransport()
        resumed.queue_json(200, [WALNUT_ITEM, STERLING_ITEM])
        self._run(resumed, "--resume")

        self.assertEqual([request.method for request in resumed.requests], ["GET"])
        self.assertEqual(parse_qs(urlsplit(resumed.requests[0].url).query)["id"], ["job-1"])
        self.assertEqual(self._saved_rows(), [STERLING_ROW, WALNUT_ROW])
        self.assertFalse(self.jobs_path.exists())

    def test_a_pending_job_gets_only_as_many_polls_as_the_budget_allows(self) -> None:
        self._write_jobs({"job-9": [WALNUT_KEY]})
        transport = FakeTransport()
        transport.queue_json(202, {"id": "job-9", "status": "pending"})

        with self.assertLogs("fuelplan", level="INFO"):
            message = self._run(transport, "--resume", "--max-credits", "1", max_polls=3)

        self.assertEqual(transport.count, 1)
        self.assertIn("Credit budget reached while waiting for job job-9", message)
        self.assertEqual(self._jobs(), {"job-9": [WALNUT_KEY]})

    def test_addresses_left_never_counts_a_pending_address_that_is_already_saved(self) -> None:
        self._write_output(OUTPUT_COLUMNS, [WALNUT_ROW[name] for name in OUTPUT_COLUMNS])
        self._write_jobs({"job-9": [STERLING_KEY, WALNUT_KEY]})
        transport = FakeTransport()
        transport.queue_json(202, {"id": "job-9", "status": "pending"})

        with self.assertLogs("fuelplan", level="INFO"):
            message = self._run(transport, "--resume", "--max-credits", "1", max_polls=3)

        self.assertIn("Addresses left: 1; rerun with --resume tomorrow.", message)

    def test_a_sample_job_over_the_budget_is_never_submitted_and_counts_only_the_sample(
        self,
    ) -> None:
        transport = FakeTransport()

        message = self._run(transport, "--sample", "1", "--max-credits", "7")  # needs 7.5

        self.assertEqual(transport.count, 0)
        self.assertIn("Addresses left: 1; rerun with --resume tomorrow.", message)

    def test_max_credits_below_one_is_rejected_before_any_request(self) -> None:
        for value in ("0", "-1", "lots"):
            transport = FakeTransport()
            with self.subTest(value=value), self.assertRaisesMessage(CommandError, "at least 1"):
                self._run(transport, "--max-credits", value)
            self.assertEqual(transport.count, 0)

    # Item 2: pending-job state file.
    def test_a_new_job_is_saved_to_the_pending_jobs_file_before_its_first_poll(self) -> None:
        transport = SnapshotTransport(jobs_path=self.jobs_path)
        _queue_job(transport, [WALNUT_ITEM, STERLING_ITEM])

        self._run(transport)

        self.assertEqual(transport.snapshots, [{"job-1": [STERLING_KEY, WALNUT_KEY]}])

    def test_an_out_path_in_a_missing_folder_saves_the_job_before_its_first_poll(self) -> None:
        self.out_path = self.workdir / "new" / "deeper" / "locations.csv"
        jobs_path = self.out_path.with_name("locations.csv.jobs.json")
        transport = SnapshotTransport(jobs_path=jobs_path)
        _queue_job(transport, [WALNUT_ITEM, STERLING_ITEM])

        message = self._run(transport)

        self.assertEqual(transport.snapshots, [{"job-1": [STERLING_KEY, WALNUT_KEY]}])
        self.assertIn("Created job job-1 for 2 addresses.", message)
        self.assertEqual(self._saved_rows(), [STERLING_ROW, WALNUT_ROW])

    def test_the_new_job_id_is_shown_even_when_saving_it_fails(self) -> None:
        transport = FakeTransport()
        transport.queue_json(202, {"id": "job-1"})
        output = StringIO()
        failing_replace = mock.patch.object(
            geocode_stations.os, "replace", side_effect=OSError("disk full")
        )

        with failing_replace, self.assertRaisesMessage(CommandError, "cannot save the pending"):
            call_command(
                "geocode_stations",
                "--out",
                str(self.out_path),
                transport=transport,
                client_options={"sleep": [].append},
                stdout=output,
            )

        self.assertIn("Created job job-1 for 2 addresses.", output.getvalue())
        self.assertEqual(transport.count, 1)

    def test_a_failed_state_write_leaves_the_previous_pending_jobs_file_unchanged(self) -> None:
        self._write_jobs({"job-8": [STERLING_KEY], "job-9": [WALNUT_KEY]})
        before = self.jobs_path.read_text(encoding="utf-8")
        transport = FakeTransport()
        transport.queue_json(200, [STERLING_ITEM])  # job-8 finishes; forgetting it fails
        failing_replace = mock.patch.object(
            geocode_stations.os, "replace", side_effect=OSError("disk full")
        )

        with failing_replace, self.assertRaisesMessage(CommandError, str(self.jobs_path)):
            self._run(transport, "--resume")

        self.assertEqual(self.jobs_path.read_text(encoding="utf-8"), before)

    def test_a_pending_job_whose_addresses_are_all_saved_is_dropped_without_a_request(
        self,
    ) -> None:
        self._write_output(OUTPUT_COLUMNS, [WALNUT_ROW[name] for name in OUTPUT_COLUMNS])
        self._write_jobs({"job-9": [WALNUT_KEY]})
        transport = FakeTransport()
        _queue_job(transport, [STERLING_ITEM])

        self._run(transport, "--resume")

        self.assertEqual([request.method for request in transport.requests], ["POST", "GET"])
        self.assertEqual(transport.sent_texts(0), [STERLING])
        self.assertEqual(parse_qs(urlsplit(transport.requests[1].url).query)["id"], ["job-1"])
        self.assertFalse(self.jobs_path.exists())

    def test_resume_after_a_crash_between_append_and_forget_makes_no_request(self) -> None:
        self._write_output(
            OUTPUT_COLUMNS,
            [STERLING_ROW[name] for name in OUTPUT_COLUMNS],
            [WALNUT_ROW[name] for name in OUTPUT_COLUMNS],
        )
        self._write_jobs({"job-9": [STERLING_KEY, WALNUT_KEY]})
        transport = FakeTransport()

        message = self._run(transport, "--resume")

        self.assertEqual(transport.count, 0)
        self.assertIn("Nothing to geocode", message)
        self.assertEqual(self._saved_rows(), [STERLING_ROW, WALNUT_ROW])
        self.assertFalse(self.jobs_path.exists())

    def test_a_second_run_on_the_same_output_stops_before_any_request(self) -> None:
        lock_path = self.workdir / "locations.csv.lock"
        with lock_path.open("a", encoding="utf-8") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            transport = FakeTransport()

            with self.assertRaisesMessage(CommandError, "another geocode_stations run is active"):
                self._run(transport)

        self.assertEqual(transport.count, 0)

    def test_a_saved_job_is_removed_from_the_pending_jobs_file(self) -> None:
        transport = FakeTransport()
        _queue_job(transport, [WALNUT_ITEM, STERLING_ITEM])

        self._run(transport)

        self.assertFalse(self.jobs_path.exists())
        self.assertEqual(list(self.workdir.glob("*.tmp")), [])

    def test_a_job_still_running_after_the_poll_limit_stays_pending_and_the_run_stops(
        self,
    ) -> None:
        transport = FakeTransport()
        transport.queue_json(202, {"id": "job-1"})
        for _poll in range(2):
            transport.queue_json(202, {"id": "job-1", "status": "pending"})

        with self.assertLogs("fuelplan", level="INFO"):
            message = self._run(transport, max_polls=2)

        self.assertIn("still processing job job-1", message)
        self.assertIn("rerun with --resume later", message)
        self.assertEqual(self._jobs(), {"job-1": [STERLING_KEY, WALNUT_KEY]})
        self.assertFalse(self.out_path.exists())

    def test_resume_fetches_a_pending_job_without_submitting_its_addresses_again(self) -> None:
        self._write_jobs({"job-9": [STERLING_KEY, WALNUT_KEY]})
        transport = FakeTransport()
        transport.queue_json(200, [WALNUT_ITEM, STERLING_ITEM])

        self._run(transport, "--resume")

        self.assertEqual([request.method for request in transport.requests], ["GET"])
        query = parse_qs(urlsplit(transport.requests[0].url).query)
        self.assertEqual(query["id"], ["job-9"])
        self.assertEqual(self._saved_rows(), [STERLING_ROW, WALNUT_ROW])
        self.assertFalse(self.jobs_path.exists())

    def test_addresses_of_a_still_running_pending_job_are_never_resubmitted(self) -> None:
        self._write_jobs({"job-9": [WALNUT_KEY]})
        transport = FakeTransport()
        transport.queue_json(202, {"id": "job-9", "status": "pending"})

        with self.assertLogs("fuelplan", level="INFO"):
            message = self._run(transport, "--resume", max_polls=1)

        self.assertEqual([request.method for request in transport.requests], ["GET"])
        self.assertIn("still processing job job-9", message)
        self.assertEqual(self._jobs(), {"job-9": [WALNUT_KEY]})

    def test_after_a_pending_job_only_the_other_addresses_are_submitted(self) -> None:
        self._write_jobs({"job-9": [WALNUT_KEY]})
        transport = FakeTransport()
        transport.queue_json(200, [WALNUT_ITEM])  # pending job-9 finishes
        _queue_job(transport, [STERLING_ITEM])

        self._run(transport, "--resume")

        self.assertEqual(transport.sent_texts(1), [STERLING])
        self.assertEqual(self._saved_rows(), [STERLING_ROW, WALNUT_ROW])

    def test_a_pending_job_already_partly_saved_never_writes_a_key_twice(self) -> None:
        self._write_output(OUTPUT_COLUMNS, [WALNUT_ROW[name] for name in OUTPUT_COLUMNS])
        self._write_jobs({"job-9": [STERLING_KEY, WALNUT_KEY]})
        transport = FakeTransport()
        transport.queue_json(200, [WALNUT_ITEM, STERLING_ITEM])

        self._run(transport, "--resume")

        self.assertEqual(self._saved_rows(), [STERLING_ROW, WALNUT_ROW])

    def test_a_failed_poll_keeps_the_job_pending_for_resume(self) -> None:
        transport = FakeTransport()
        transport.queue_json(202, {"id": "job-1"})
        transport.queue_json(500, {"error": "boom"})

        with self.assertRaisesMessage(CommandError, "job job-1 is kept in"):
            self._run(transport)

        self.assertEqual(self._jobs(), {"job-1": [STERLING_KEY, WALNUT_KEY]})

    def test_a_pending_jobs_file_is_never_ignored_without_resume(self) -> None:
        self._write_jobs({"job-9": [WALNUT_KEY]})
        transport = FakeTransport()

        with self.assertRaisesMessage(CommandError, "jobs.json exists; pass --resume"):
            self._run(transport)

        self.assertEqual(transport.count, 0)

    # Item 3: polling interval.
    def test_the_command_waits_10_seconds_between_polls_for_a_small_batch(self) -> None:
        transport = FakeTransport()
        _queue_job(transport, [WALNUT_ITEM, STERLING_ITEM])
        sleeps: list[float] = []

        self._run(transport, sleeps=sleeps)

        self.assertEqual(sleeps, [10])

    # Item 4: representative, stable sample.
    def test_resuming_a_sample_never_adds_addresses_beyond_the_original_sample(self) -> None:
        first = FakeTransport()
        _queue_job(first, [STERLING_ITEM])
        self._run(first, "--sample", "1")
        second = FakeTransport()

        message = self._run(second, "--sample", "1", "--resume")

        self.assertEqual(first.sent_texts(0), [STERLING])
        self.assertEqual(second.count, 0)
        self.assertIn("Nothing to geocode", message)

    # Item 7: clean file errors.
    def test_a_missing_input_csv_stops_the_command_with_a_clear_error(self) -> None:
        self.csv_path.unlink()

        with self.assertRaisesMessage(
            CommandError, f"cannot read the fuel-price CSV {self.csv_path}"
        ):
            self._run(FakeTransport())

    def test_an_output_file_with_the_wrong_header_stops_the_command_naming_it(self) -> None:
        self._write_output(OUTPUT_COLUMNS[:8], [WALNUT_KEY, "1", "2", "", "", "", "", ""])

        with self.assertRaisesMessage(CommandError, f"{self.out_path} does not have the expected"):
            self._run(FakeTransport(), "--resume")

    def test_an_output_row_with_the_wrong_number_of_columns_stops_the_command(self) -> None:
        self._write_output(OUTPUT_COLUMNS, [WALNUT_KEY, "41.48"])

        with self.assertRaisesMessage(CommandError, f"{self.out_path} has a row with the wrong"):
            self._run(FakeTransport(), "--resume")

    def test_an_output_file_with_a_cut_off_last_line_stops_the_command_naming_it(self) -> None:
        self._write_output(OUTPUT_COLUMNS, [WALNUT_ROW[name] for name in OUTPUT_COLUMNS])
        content = self.out_path.read_text(encoding="utf-8").rstrip("\r\n")
        self.out_path.write_text(content, encoding="utf-8")
        transport = FakeTransport()

        with self.assertRaisesMessage(CommandError, f"{self.out_path} does not end with a newline"):
            self._run(transport, "--resume")

        self.assertEqual(transport.count, 0)

    def test_a_pending_jobs_file_with_the_wrong_shape_stops_the_command_naming_it(self) -> None:
        for content in ("{not json", "[]", '{"job-1": "KEY"}', '{"job-1": []}', '{"job-1": [1]}'):
            self.jobs_path.write_text(content, encoding="utf-8")
            transport = FakeTransport()
            with (
                self.subTest(content=content),
                self.assertRaisesMessage(CommandError, str(self.jobs_path)),
            ):
                self._run(transport, "--resume")
            self.assertEqual(transport.count, 0)

    def test_a_pending_job_naming_an_address_not_in_the_csv_stops_the_command(self) -> None:
        self._write_jobs({"job-9": ["9 NOWHERE RD|NOWHERE|KS"]})

        with self.assertRaisesMessage(CommandError, "1 address keys that are not in the CSV"):
            self._run(FakeTransport(), "--resume")

    # Items 6 and 8 at command level.
    def test_metadata_without_coordinates_is_saved_with_empty_lat_and_lon(self) -> None:
        transport = FakeTransport()
        no_point = {**WALNUT_ITEM, "lat": None, "lon": None, "state_code": None}
        _queue_job(transport, [no_point, STERLING_ITEM])

        self._run(transport)

        expected = {**WALNUT_ROW, "lat": "", "lon": "", "state_code": ""}
        self.assertEqual(self._saved_rows(), [STERLING_ROW, expected])

    @override_settings(GEOAPIFY_BASE_URL="http://api.geoapify.com")
    def test_a_plain_http_geoapify_url_stops_the_command_before_any_request(self) -> None:
        transport = FakeTransport()

        with self.assertRaisesMessage(CommandError, "GEOAPIFY_BASE_URL must use https"):
            self._run(transport)

        self.assertEqual(transport.count, 0)

    def _use_batch_size(self, size: int) -> None:
        patcher = mock.patch.object(geocode_stations, "MAX_BATCH_SIZE", size)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _jobs(self) -> dict[str, list[str]]:
        return json.loads(self.jobs_path.read_text(encoding="utf-8"))


class SampleTests(OfflineTestCase):
    def test_sample_spreads_over_the_sorted_addresses(self) -> None:
        keys = [f"KEY{index:02d}" for index in range(10)]

        self.assertEqual(pick_sample(keys, 3), ["KEY00", "KEY03", "KEY06"])
        self.assertEqual(pick_sample(keys, 50), sorted(keys))

    def test_highway_style_addresses_are_recognised_from_the_address_part_only(self) -> None:
        highway = ["I-80, EXIT 46|WALNUT|IA", "US-30 & SR-2|A|NE", "HWY 50|B|KS", "I-10/I-20|C|TX"]
        ordinary = ["123 MAIN ST|STERLING|CO", "1 EXITER AVE|D|OH", "5 OAK RD|EXIT|AL"]

        self.assertEqual([is_highway_style(key) for key in highway], [True] * 4)
        self.assertEqual([is_highway_style(key) for key in ordinary], [False] * 3)

    def test_a_sample_is_half_highway_style_and_half_ordinary(self) -> None:
        highway = [f"I-{number}, EXIT 1|CITY|IA" for number in range(10, 20)]
        ordinary = [f"{number} MAIN ST|CITY|CO" for number in range(10, 20)]

        sample = pick_sample(highway + ordinary, 4)

        self.assertEqual(sum(is_highway_style(key) for key in sample), 2)
        self.assertEqual(len(sample), 4)

    def test_a_small_group_is_topped_up_by_the_other_one(self) -> None:
        keys = ["I-80, EXIT 1|A|IA", "1 MAIN ST|A|CO", "2 MAIN ST|B|CO", "3 MAIN ST|C|CO"]

        self.assertEqual(len(pick_sample(keys, 4)), 4)
        self.assertEqual(sum(is_highway_style(key) for key in pick_sample(keys, 3)), 1)

    def test_done_keys_are_removed_after_picking_so_the_sample_never_grows(self) -> None:
        keys = [f"{number} MAIN ST|CITY|CO" for number in range(10, 30)]
        sample = pick_sample(keys, 4)

        todo = select_todo(all_keys=keys, sample_size=4, skip=set(sample[:3]))

        self.assertEqual(todo, sample[3:])


class RedactUrlTests(OfflineTestCase):
    def test_redact_url_hides_the_api_key_in_the_middle(self) -> None:
        url = f"https://api.geoapify.com/v1/batch/geocode/search?id=1&apiKey={API_KEY}&format=json"

        self.assertEqual(
            redact_url(url),
            "https://api.geoapify.com/v1/batch/geocode/search?id=1&apiKey=***&format=json",
        )

    def test_redact_url_hides_the_api_key_at_the_end(self) -> None:
        url = f"https://api.geoapify.com/v1/batch/geocode/search?filter=countrycode%3Aus&apiKey={API_KEY}"

        self.assertEqual(
            redact_url(url),
            "https://api.geoapify.com/v1/batch/geocode/search?filter=countrycode%3Aus&apiKey=***",
        )
