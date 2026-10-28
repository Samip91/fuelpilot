"""Shared test helpers: no test may reach the network (spec 01 NFR-8)."""

import copy
import json
import socket
from dataclasses import dataclass, field
from datetime import UTC, timedelta
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse as DjangoResponse
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.throttling import AnonRateThrottle

from fuelplan.http import HttpResponse
from fuelplan.planner import Meta, PlanOutcome

LOOPBACK_HOST = "127.0.0.1"
PLAN_URL = "/api/v1/fuel-plans"
_real_connect = socket.socket.connect


class NetworkAccessBlocked(AssertionError):
    """Raised when code under test tries to open a real network connection."""


def _blocked_connect(sock: socket.socket, address: object) -> None:
    sock.close()  # the caller never gets the socket back, so close it here (no leak warning)
    raise NetworkAccessBlocked("tests must not open network connections; use a fake transport")


def _loopback_only_connect(sock: socket.socket, address: object) -> None:
    """Allow connections to a local test server on 127.0.0.1 and nothing else."""
    if isinstance(address, tuple) and address[0] == LOOPBACK_HOST:
        return _real_connect(sock, address)
    return _blocked_connect(sock, address)


class _NetworkBlockMixin:
    """Fails any real socket connect for the whole test (loopback only if allowed)."""

    allow_loopback: bool = False

    def setUp(self) -> None:
        super().setUp()
        connect = _loopback_only_connect if self.allow_loopback else _blocked_connect
        patcher = mock.patch.object(socket.socket, "connect", connect)
        patcher.start()
        self.addCleanup(patcher.stop)


class OfflineTestCase(_NetworkBlockMixin, TestCase):
    """TestCase that fails any real socket connect for the whole test.

    A test class may set `allow_loopback = True` to talk to its own server on
    127.0.0.1 (the transport deadline tests); every other address stays blocked.
    """


class OfflineTransactionTestCase(_NetworkBlockMixin, TransactionTestCase):
    """The same network block for tests that cannot run inside a transaction (migrations)."""


@dataclass
class RecordedRequest:
    method: str
    url: str
    body: bytes | None
    connect_timeout_s: float | None = None
    total_timeout_s: float | None = None
    max_bytes: int | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeTransport:
    """Replays queued responses (or raises queued errors) and records every request.

    Tests assert `count` explicitly: an empty queue raises AssertionError, which a
    client catching every exception could otherwise hide behind a 502.
    """

    responses: list[HttpResponse | Exception] = field(default_factory=list)
    requests: list[RecordedRequest] = field(default_factory=list)

    def queue_json(
        self, status: int, payload: object, *, headers: dict[str, str] | None = None
    ) -> None:
        self.queue_response(status, json.dumps(payload).encode(), headers=headers)

    def queue_body(self, status: int, body: bytes) -> None:
        self.queue_response(status, body)

    def queue_response(
        self, status: int, body: bytes, *, headers: dict[str, str] | None = None
    ) -> None:
        """Queue a reply; header names are lower-cased like the real transport does."""
        lowered = {name.lower(): value for name, value in (headers or {}).items()}
        self.responses.append(HttpResponse(status=status, headers=lowered, body=body))

    def queue_exception(self, error: Exception) -> None:
        self.responses.append(error)

    def request(
        self,
        *,
        method: str,
        url: str,
        body: bytes | None = None,
        connect_timeout_s: float | None = None,
        total_timeout_s: float | None = None,
        max_bytes: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        self.requests.append(
            RecordedRequest(
                method=method,
                url=url,
                body=body,
                connect_timeout_s=connect_timeout_s,
                total_timeout_s=total_timeout_s,
                max_bytes=max_bytes,
                headers=dict(headers or {}),
            )
        )
        if not self.responses:
            raise AssertionError(f"unexpected request {method} {url}")
        reply = self.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def count(self) -> int:
        return len(self.requests)

    def sent_texts(self, index: int) -> list[str]:
        """The address texts posted in request number `index`."""
        return json.loads(self.requests[index].body or b"[]")


class FakeClock:
    """A clock that only moves when told to: `sleep` advances wall and monotonic time."""

    def __init__(self, *, now: float = 1_000.0) -> None:
        self._now = now
        self._monotonic = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._now += seconds
        self._monotonic += seconds


def assert_envelope(test: TestCase, response: DjangoResponse, *, status: int, code: str) -> dict:
    """Check the §7a error envelope and return its `details`."""
    test.assertEqual(response.status_code, status, response.content)
    test.assertEqual(response["Content-Type"], "application/json")
    test.assertNotIn(b"Traceback", response.content)
    body = json.loads(response.content)
    test.assertEqual(set(body), {"error"})
    test.assertEqual(set(body["error"]), {"code", "message", "details"})
    test.assertEqual(body["error"]["code"], code)
    test.assertIsInstance(body["error"]["message"], str)
    test.assertTrue(body["error"]["message"])
    test.assertIsInstance(body["error"]["details"], dict)
    return body["error"]["details"]


STUB_PLAN_ID = "00000000-0000-4000-8000-000000000000"
CACHE_TABLE = "fuelpilot_cache"
CACHE_KEY_PREFIX = ":1:"  # Django's default key prefix and version


STUB_ENDPOINT = {"matched": "Stubville, TX", "location": {"type": "Point", "coordinates": [0, 0]}}
# Only the fields the response summary reads, so the view can build a full answer.
STUB_PLAN_BODY = {
    "plan_id": STUB_PLAN_ID,
    "reached": "planner",
    "start": STUB_ENDPOINT,
    "finish": STUB_ENDPOINT,
    "route": {"distance_miles": "0.0"},
    "fuel_stops": [],
    "finish_fuel_on_arrival_gallons": "50.0",
    "totals": {"gallons_bought": 0, "total_purchase_cost": "0.00"},
    "assumptions": {"start_fuel_gallons": "50.0"},
}


def stub_outcome(*, routing_calls: int = 0) -> PlanOutcome:
    """A minimal planner answer for tests that stop at the view (validation, call counts)."""
    return PlanOutcome(
        plan_id=STUB_PLAN_ID,
        body=copy.deepcopy(STUB_PLAN_BODY),
        meta=Meta(routing_calls=routing_calls, route_cache="miss", plan_cache="miss"),
    )


def throttle_cache_key() -> str:
    """The cache key DRF's anonymous throttle uses for test requests from 127.0.0.1."""
    return AnonRateThrottle.cache_format % {"scope": "anon", "ident": LOOPBACK_HOST}


def cache_keys() -> set[str]:
    """Every key in the database cache, without Django's ":1:" prefix (routes, plans...)."""
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT cache_key FROM {CACHE_TABLE}")
        return {key.removeprefix(CACHE_KEY_PREFIX) for (key,) in cursor.fetchall()}


def ttl_of(key: str) -> float:
    """Seconds until `key` (without the ":1:" prefix) expires in the database cache."""
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT expires FROM {CACHE_TABLE} WHERE cache_key = %s", [CACHE_KEY_PREFIX + key]
        )
        (expires,) = cursor.fetchone()
    if timezone.is_naive(expires):  # SQLite returns the stored UTC time without a zone
        expires = timezone.make_aware(expires, UTC)
    return (expires - timezone.now()).total_seconds()


def expire_cache_key(key: str) -> None:
    """Make `key` look expired, as if its TTL had passed (FR-37 route expiry)."""
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {CACHE_TABLE} SET expires = %s WHERE cache_key = %s",
            [
                connection.ops.adapt_datetimefield_value(timezone.now() - timedelta(seconds=1)),
                CACHE_KEY_PREFIX + key,
            ],
        )


class ApiTestCase(OfflineTestCase):
    """HTTP tests of the plan endpoint.

    Our POST throttle (30/min per IP) keeps its counts in the cache, and every test
    request comes from 127.0.0.1. So each `post_*` call empties the cache first; with
    `keep_cache=True` only the throttle count is reset, so saved routes and plans stay.
    The throttle test sends requests without either reset.
    """

    def post_raw(
        self,
        body: bytes,
        *,
        content_type: str = "application/json",
        keep_cache: bool = False,
        **extra: str,
    ) -> DjangoResponse:
        if keep_cache:
            cache.delete(throttle_cache_key())
        else:
            cache.clear()
        return self.client.generic("POST", PLAN_URL, body, content_type=content_type, **extra)

    def post_json(
        self, payload: object, *, keep_cache: bool = False, **extra: str
    ) -> DjangoResponse:
        return self.post_raw(json.dumps(payload).encode(), keep_cache=keep_cache, **extra)
