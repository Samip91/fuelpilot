"""Geoapify batch geocoding for the owner-only `geocode_stations` command (spec 01 FR-17).

This module only fetches and matches raw results; labels are applied at import time.
A job is two steps on purpose: `submit` returns a job id the caller saves before any
waiting, and `fetch` can then be retried later for that id without paying for a new job.
"""

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import SplitResult, urlencode, urlsplit

from .http import ALLOWED_SCHEMES, HttpResponse, Transport, TransportError, redact_url

logger = logging.getLogger(__name__)

# Base URL and timeouts come from settings (GEOAPIFY_*); these are API facts and safety caps.
BATCH_PATH = "/v1/batch/geocode/search"
MAX_BATCH_SIZE = 1_000
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
# Geoapify's own batch sample waits 60 s between result requests; its 10 s example is for
# a small job, so only batches up to SMALL_BATCH_MAX_ADDRESSES poll that often.
POLL_INTERVAL_S = 60
SMALL_BATCH_POLL_INTERVAL_S = 10
SMALL_BATCH_MAX_ADDRESSES = 100
DEFAULT_MAX_POLLS = 60
# The API key travels in the URL, so plain http is allowed only for local test servers.
PLAIN_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1"})
PLAIN_HTTP_TOP_LABEL = "test"  # reserved for testing (RFC 2606), so never a real host


class GeocodingError(Exception):
    """The batch job could not be created, fetched or matched."""


@dataclass(frozen=True, slots=True)
class GeocodeResult:
    """Raw Geoapify fields for one address; empty lat/lon mean "no result"."""

    query_text: str
    lat: str
    lon: str
    result_type: str
    confidence: str
    state_code: str
    attribution: str
    license: str
    source_url: str


@dataclass(frozen=True, slots=True)
class JobFetch:
    """One fetch of a job: its items (None while still running) and the result requests made."""

    items: list[dict[str, object]] | None
    polls: int


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """Results for the texts Geoapify answered, and the texts it left out."""

    results: list[GeocodeResult]
    unmatched_texts: list[str]


def poll_interval_s(address_count: int) -> int:
    """Seconds to wait before each result request; small jobs finish fast, so poll sooner."""
    if address_count <= SMALL_BATCH_MAX_ADDRESSES:
        return SMALL_BATCH_POLL_INTERVAL_S
    return POLL_INTERVAL_S


class GeoapifyBatchClient:
    """Creates Geoapify batch jobs and fetches their results; one HTTP call per step."""

    def __init__(
        self,
        *,
        transport: Transport,
        api_key: str,
        base_url: str,
        connect_timeout_s: float,
        total_timeout_s: float,
        max_polls: int = DEFAULT_MAX_POLLS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise GeocodingError("GEOAPIFY_API_KEY is not set (see .env.example)")
        require_safe_base_url(base_url)
        self._transport = transport
        self._api_key = api_key
        self._batch_url = f"{base_url.rstrip('/')}{BATCH_PATH}"
        self._connect_timeout_s = connect_timeout_s
        self._total_timeout_s = total_timeout_s
        self.max_polls = max_polls
        self._sleep = sleep

    def submit(self, texts: Sequence[str]) -> str:
        """Create one job for unique texts and return its id (costs credits; no waiting)."""
        if not 0 < len(texts) <= MAX_BATCH_SIZE:
            raise GeocodingError(f"a batch needs 1 to {MAX_BATCH_SIZE} addresses")
        if len(set(texts)) != len(texts):
            raise GeocodingError("a batch must not contain the same address text twice")
        url = self._url(filter="countrycode:us")
        response = self._send("POST", url, body=json.dumps(list(texts)).encode())
        if response.status not in (200, 202):
            raise GeocodingError(f"job creation failed with HTTP {response.status}")
        payload = _load_json(response)
        job_id = payload.get("id") if isinstance(payload, dict) else None
        if not job_id:
            raise GeocodingError("job creation returned no job id")
        return str(job_id)

    def fetch(self, job_id: str, *, address_count: int, poll_limit: int) -> JobFetch:
        """Make at most `poll_limit` result requests (never more than `max_polls`).

        A job still running after the last allowed request is returned unfinished, so the
        caller can keep its id and fetch it again later.
        """
        url = self._url(id=job_id, format="json")
        interval_s = poll_interval_s(address_count)
        polls_allowed = min(poll_limit, self.max_polls)
        for poll in range(1, polls_allowed + 1):
            self._sleep(interval_s)
            response = self._send("GET", url)
            if response.status == 202:
                logger.info("Geoapify job %s still running (poll %s)", job_id, poll)
                continue
            if response.status != 200:
                raise GeocodingError(f"result request failed with HTTP {response.status}")
            payload = _load_json(response)
            if not isinstance(payload, list):
                raise GeocodingError("result payload is not a list")
            items = [item for item in payload if isinstance(item, dict)]
            return JobFetch(items=items, polls=poll)
        return JobFetch(items=None, polls=polls_allowed)

    def _url(self, **params: str) -> str:
        return f"{self._batch_url}?{urlencode({**params, 'apiKey': self._api_key})}"

    def _send(self, method: str, url: str, body: bytes | None = None) -> HttpResponse:
        try:
            return self._transport.request(
                method=method,
                url=url,
                body=body,
                headers={"Content-Type": "application/json"} if body else None,
                connect_timeout_s=self._connect_timeout_s,
                total_timeout_s=self._total_timeout_s,
                max_bytes=MAX_RESPONSE_BYTES,
            )
        except TransportError as error:
            # `from None`: the original error may quote the keyed URL, so it is not
            # chained into tracebacks; its text is kept here, redacted.
            raise GeocodingError(
                f"{method} {redact_url(url)} failed: {redact_url(str(error))}"
            ) from None


def match_results(*, texts: Sequence[str], items: Sequence[dict[str, object]]) -> BatchOutcome:
    """Match items to sent texts by the echoed query text, never by position."""
    by_text = {_query_text(item): item for item in items}
    matched = [text for text in texts if text in by_text]
    if items and not matched:
        raise GeocodingError("no result in the reply matches any address that was sent")
    return BatchOutcome(
        results=[_to_result(text, by_text[text]) for text in matched],
        unmatched_texts=[text for text in texts if text not in by_text],
    )


def require_safe_base_url(base_url: str) -> None:
    """Refuse a base URL that could leak the key or call the wrong place, before any request.

    Messages never echo the URL, because a bad one may carry a username or password.
    """
    try:
        parts = urlsplit(base_url)
        hostname = parts.hostname
        _ = parts.port  # raises ValueError for a port that is not a number
    except ValueError:
        raise GeocodingError("GEOAPIFY_BASE_URL must be an http(s) URL with a host") from None
    if _has_extra_parts(parts):
        raise GeocodingError(
            "GEOAPIFY_BASE_URL must not contain a query, fragment, username or password"
        )
    if parts.scheme not in ALLOWED_SCHEMES or not hostname:
        raise GeocodingError("GEOAPIFY_BASE_URL must be an http(s) URL with a host")
    if parts.scheme == "http" and not _is_local_test_host(hostname):
        raise GeocodingError(
            "GEOAPIFY_BASE_URL must use https, because the API key is sent in the URL "
            "(http only for localhost, 127.0.0.1 and *.test hosts)"
        )


def _has_extra_parts(parts: SplitResult) -> bool:
    return bool(parts.query or parts.fragment) or "@" in parts.netloc


def _is_local_test_host(hostname: str) -> bool:
    """`localhost`, `127.0.0.1` or `<name>.test`; a bare `.test` or `latest` is refused."""
    labels = hostname.split(".")
    is_dot_test = len(labels) >= 2 and labels[-1] == PLAIN_HTTP_TOP_LABEL and all(labels)
    return hostname in PLAIN_HTTP_HOSTS or is_dot_test


def _load_json(response: HttpResponse) -> object:
    try:
        return json.loads(response.body)
    except ValueError as error:
        raise GeocodingError("Geoapify returned a body that is not JSON") from error


def _query_text(item: dict[str, object]) -> str:
    query = item.get("query")
    return str(query.get("text", "")) if isinstance(query, dict) else ""


def _text(value: object) -> str:
    """A provider value as CSV text; a missing value is "" (never the word "None")."""
    return "" if value is None else str(value)


def _field(item: dict[str, object], group: str, name: str) -> str:
    """A nested field such as `rank.confidence`, or "" when the group is missing."""
    values = item.get(group)
    return _text(values.get(name)) if isinstance(values, dict) else ""


def _to_result(text: str, item: dict[str, object]) -> GeocodeResult:
    # Every field is kept even without coordinates; empty lat/lon alone mean "no result".
    has_point = item.get("lat") is not None and item.get("lon") is not None
    return GeocodeResult(
        query_text=text,
        lat=_text(item.get("lat")) if has_point else "",
        lon=_text(item.get("lon")) if has_point else "",
        result_type=_text(item.get("result_type")),
        confidence=_field(item, "rank", "confidence"),
        state_code=_text(item.get("state_code")).upper(),
        attribution=_field(item, "datasource", "attribution"),
        license=_field(item, "datasource", "license"),
        source_url=_field(item, "datasource", "url"),
    )
