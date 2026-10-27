"""Spec 01 FR-17: the Geoapify batch client fails clearly on every bad reply."""

from fuelplan.geocoding import (
    POLL_INTERVAL_S,
    SMALL_BATCH_POLL_INTERVAL_S,
    GeoapifyBatchClient,
    GeocodeResult,
    GeocodingError,
    match_results,
)

from .base import FakeTransport, OfflineTestCase

TEXTS = ["1 MAIN ST, Omaha, NE, United States"]
ENDPOINT = {
    "base_url": "https://api.geoapify.com",
    "connect_timeout_s": 10,
    "total_timeout_s": 30,
}


class GeoapifyBatchClientTests(OfflineTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.transport = FakeTransport()
        self.sleeps: list[float] = []

    def _client(self, *, max_polls: int = 3) -> GeoapifyBatchClient:
        return GeoapifyBatchClient(
            transport=self.transport,
            api_key="test-key",
            **ENDPOINT,
            max_polls=max_polls,
            sleep=self.sleeps.append,
        )

    def test_a_job_still_running_after_every_poll_is_returned_unfinished(self) -> None:
        for _poll in range(3):
            self.transport.queue_json(202, {"id": "job-1", "status": "pending"})

        with self.assertLogs("fuelplan", level="INFO") as logs:
            fetched = self._client(max_polls=3).fetch("job-1", address_count=1, poll_limit=60)

        self.assertIsNone(fetched.items)
        self.assertEqual(fetched.polls, 3)
        self.assertEqual(self.transport.count, 3)
        self.assertEqual(len(logs.output), 3)

    def test_fetch_never_makes_more_result_requests_than_the_poll_limit(self) -> None:
        for _poll in range(3):
            self.transport.queue_json(202, {"id": "job-1", "status": "pending"})

        with self.assertLogs("fuelplan", level="INFO"):
            fetched = self._client(max_polls=3).fetch("job-1", address_count=1, poll_limit=2)

        self.assertIsNone(fetched.items)
        self.assertEqual(fetched.polls, 2)
        self.assertEqual(self.transport.count, 2)

    def test_a_finished_job_returns_its_items_and_the_number_of_result_requests(self) -> None:
        self.transport.queue_json(202, {"id": "job-1", "status": "pending"})
        self.transport.queue_json(200, [{"query": {"text": TEXTS[0]}}, "not an item"])

        with self.assertLogs("fuelplan", level="INFO"):
            fetched = self._client().fetch("job-1", address_count=1, poll_limit=60)

        self.assertEqual(fetched.items, [{"query": {"text": TEXTS[0]}}])
        self.assertEqual(fetched.polls, 2)

    def test_submit_returns_the_job_id_with_one_request_and_no_waiting(self) -> None:
        self.transport.queue_json(202, {"id": "job-7", "status": "pending"})

        job_id = self._client().submit(TEXTS)

        self.assertEqual(job_id, "job-7")
        self.assertEqual(self.transport.count, 1)
        self.assertEqual(self.sleeps, [])

    def test_a_batch_of_up_to_100_addresses_is_polled_every_10_seconds(self) -> None:
        self.transport.queue_json(200, [])

        self._client().fetch("job-1", address_count=100, poll_limit=60)

        self.assertEqual(self.sleeps, [SMALL_BATCH_POLL_INTERVAL_S])
        self.assertEqual(SMALL_BATCH_POLL_INTERVAL_S, 10)

    def test_a_batch_of_more_than_100_addresses_is_polled_every_60_seconds(self) -> None:
        self.transport.queue_json(202, {"id": "job-1", "status": "pending"})
        self.transport.queue_json(200, [])

        with self.assertLogs("fuelplan", level="INFO"):
            self._client().fetch("job-1", address_count=101, poll_limit=60)

        self.assertEqual(self.sleeps, [POLL_INTERVAL_S, POLL_INTERVAL_S])
        self.assertEqual(POLL_INTERVAL_S, 60)

    def test_a_job_creation_reply_without_an_id_fails(self) -> None:
        self.transport.queue_json(200, {"status": "pending"})

        with self.assertRaisesMessage(GeocodingError, "no job id"):
            self._client().submit(TEXTS)

    def test_a_reply_that_is_not_json_fails(self) -> None:
        self.transport.queue_body(202, b"<html>busy</html>")

        with self.assertRaisesMessage(GeocodingError, "not JSON"):
            self._client().submit(TEXTS)

    def test_a_result_payload_that_is_not_a_list_fails(self) -> None:
        self.transport.queue_json(200, {"results": []})

        with self.assertRaisesMessage(GeocodingError, "not a list"):
            self._client().fetch("job-1", address_count=1, poll_limit=60)

    def test_an_error_status_while_polling_fails_with_that_status(self) -> None:
        for status in (429, 404):
            transport = FakeTransport()
            transport.queue_json(status, {"error": "no"})
            client = GeoapifyBatchClient(
                transport=transport,
                api_key="test-key",
                **ENDPOINT,
                sleep=lambda _seconds: None,
            )

            with (
                self.subTest(status=status),
                self.assertRaisesMessage(GeocodingError, f"HTTP {status}"),
            ):
                client.fetch("job-1", address_count=1, poll_limit=60)

    def test_an_empty_api_key_is_refused(self) -> None:
        with self.assertRaisesMessage(GeocodingError, "GEOAPIFY_API_KEY is not set"):
            GeoapifyBatchClient(transport=self.transport, api_key="", **ENDPOINT)

    def test_the_same_text_twice_in_one_batch_is_refused_before_any_request(self) -> None:
        with self.assertRaisesMessage(GeocodingError, "same address text twice"):
            self._client().submit(TEXTS * 2)

        self.assertEqual(self.transport.count, 0)

    def test_every_request_uses_the_configured_base_url_and_timeouts(self) -> None:
        self.transport.queue_json(202, {"id": "job-1"})
        self.transport.queue_json(200, [])
        client = GeoapifyBatchClient(
            transport=self.transport,
            api_key="test-key",
            base_url="http://geo.test:8080/",
            connect_timeout_s=4,
            total_timeout_s=45,
            sleep=self.sleeps.append,
        )

        client.fetch(client.submit(TEXTS), address_count=1, poll_limit=60)

        self.assertEqual(self.transport.count, 2)
        for request in self.transport.requests:
            self.assertTrue(
                request.url.startswith("http://geo.test:8080/v1/batch/geocode/search?"),
                request.url,
            )
            self.assertEqual(request.connect_timeout_s, 4)
            self.assertEqual(request.total_timeout_s, 45)


class BaseUrlSafetyTests(OfflineTestCase):
    """Items 8, 9, 11: the key is only ever sent over https to a plain host URL."""

    def _refuses(self, base_url: str, message: str) -> None:
        transport = FakeTransport()
        with (
            self.subTest(base_url=base_url),
            self.assertRaisesMessage(GeocodingError, message),
        ):
            GeoapifyBatchClient(
                transport=transport, api_key="test-key", **{**ENDPOINT, "base_url": base_url}
            )
        self.assertEqual(transport.count, 0)

    def test_a_base_url_without_http_scheme_or_host_is_refused_before_any_request(self) -> None:
        for base_url in (
            "ftp://api.geoapify.com",
            "file:///etc/passwd",
            "api.geoapify.com",
            "",
            "http://",
            "https:///v1",
            "https://api.geoapify.com:port",
        ):
            self._refuses(base_url, "GEOAPIFY_BASE_URL must be an http(s) URL with a host")

    def test_plain_http_is_refused_for_a_real_host_because_the_key_is_in_the_url(self) -> None:
        for base_url in ("http://api.geoapify.com", "http://geo.test.example.com"):
            self._refuses(base_url, "GEOAPIFY_BASE_URL must use https")

    def test_plain_http_is_refused_for_a_host_that_only_looks_like_dot_test(self) -> None:
        for base_url in ("http://latest", "http://.test", "http://geo..test"):
            self._refuses(base_url, "GEOAPIFY_BASE_URL must use https")

    def test_plain_http_is_allowed_only_for_local_and_dot_test_hosts(self) -> None:
        for base_url in ("http://localhost:8000", "http://127.0.0.1", "http://geo.test:8080"):
            with self.subTest(base_url=base_url):
                GeoapifyBatchClient(
                    transport=FakeTransport(),
                    api_key="test-key",
                    **{**ENDPOINT, "base_url": base_url},
                )

    def test_a_base_url_with_a_query_fragment_or_credentials_is_refused_without_echoing_them(
        self,
    ) -> None:
        for base_url in (
            "https://api.geoapify.com/?apiKey=s3cret",
            "https://api.geoapify.com/#s3cret",
            "https://owner:s3cret@api.geoapify.com",
            "https://s3cret@api.geoapify.com",
            "ftp://owner:s3cret@api.geoapify.com",
        ):
            transport = FakeTransport()
            with (
                self.subTest(base_url=base_url),
                self.assertRaises(GeocodingError) as raised,
            ):
                GeoapifyBatchClient(
                    transport=transport,
                    api_key="test-key",
                    **{**ENDPOINT, "base_url": base_url},
                )

            message = str(raised.exception)
            self.assertIn("must not contain a query, fragment, username or password", message)
            self.assertNotIn("s3cret", message)
            self.assertEqual(transport.count, 0)


class MatchResultsTests(OfflineTestCase):
    def test_metadata_is_kept_when_coordinates_are_missing(self) -> None:
        item = {
            "query": {"text": TEXTS[0]},
            "result_type": "city",
            "rank": {"confidence": 0.2},
            "state_code": "ne",
            "datasource": {
                "attribution": "© OpenStreetMap contributors",
                "license": "Open Database License",
                "url": "https://www.openstreetmap.org/copyright",
            },
        }

        outcome = match_results(texts=TEXTS, items=[item])

        self.assertEqual(
            outcome.results,
            [
                GeocodeResult(
                    query_text=TEXTS[0],
                    lat="",
                    lon="",
                    result_type="city",
                    confidence="0.2",
                    state_code="NE",
                    attribution="© OpenStreetMap contributors",
                    license="Open Database License",
                    source_url="https://www.openstreetmap.org/copyright",
                )
            ],
        )

    def test_null_provider_values_become_empty_text_never_the_word_none(self) -> None:
        item = {
            "query": {"text": TEXTS[0]},
            "lat": 41.2,
            "lon": None,
            "result_type": None,
            "rank": {"confidence": None},
            "state_code": None,
            "datasource": {"attribution": None, "license": None, "url": None},
        }

        result = match_results(texts=TEXTS, items=[item]).results[0]

        self.assertEqual(
            [
                result.lat,
                result.lon,
                result.result_type,
                result.confidence,
                result.state_code,
                result.attribution,
                result.license,
                result.source_url,
            ],
            [""] * 8,
        )

    def test_a_text_missing_from_the_reply_is_returned_as_unmatched(self) -> None:
        other = "2 OAK ST, Lincoln, NE, United States"

        outcome = match_results(texts=[*TEXTS, other], items=[{"query": {"text": other}}])

        self.assertEqual([result.query_text for result in outcome.results], [other])
        self.assertEqual(outcome.unmatched_texts, TEXTS)
