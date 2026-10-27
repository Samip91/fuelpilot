"""Shared test helpers: no test may reach the network (spec 01 NFR-8)."""

import json
import socket
from dataclasses import dataclass, field
from unittest import mock

from django.test import TestCase

from fuelplan.http import HttpResponse

LOOPBACK_HOST = "127.0.0.1"
_real_connect = socket.socket.connect


class NetworkAccessBlocked(AssertionError):
    """Raised when code under test tries to open a real network connection."""


def _blocked_connect(sock: socket.socket, address: object) -> None:
    raise NetworkAccessBlocked("tests must not open network connections; use a fake transport")


def _loopback_only_connect(sock: socket.socket, address: object) -> None:
    """Allow connections to a local test server on 127.0.0.1 and nothing else."""
    if isinstance(address, tuple) and address[0] == LOOPBACK_HOST:
        return _real_connect(sock, address)
    return _blocked_connect(sock, address)


class OfflineTestCase(TestCase):
    """TestCase that fails any real socket connect for the whole test.

    A test class may set `allow_loopback = True` to talk to its own server on
    127.0.0.1 (the transport deadline tests); every other address stays blocked.
    """

    allow_loopback: bool = False

    def setUp(self) -> None:
        super().setUp()
        connect = _loopback_only_connect if self.allow_loopback else _blocked_connect
        patcher = mock.patch.object(socket.socket, "connect", connect)
        patcher.start()
        self.addCleanup(patcher.stop)


@dataclass
class RecordedRequest:
    method: str
    url: str
    body: bytes | None
    connect_timeout_s: float | None = None
    total_timeout_s: float | None = None


@dataclass
class FakeTransport:
    """Replays queued responses and records every request it receives."""

    responses: list[HttpResponse] = field(default_factory=list)
    requests: list[RecordedRequest] = field(default_factory=list)

    def queue_json(self, status: int, payload: object) -> None:
        self.queue_body(status, json.dumps(payload).encode())

    def queue_body(self, status: int, body: bytes) -> None:
        self.responses.append(HttpResponse(status=status, headers={}, body=body))

    def request(
        self,
        *,
        method: str,
        url: str,
        body: bytes | None = None,
        connect_timeout_s: float | None = None,
        total_timeout_s: float | None = None,
        **_: object,
    ) -> HttpResponse:
        self.requests.append(
            RecordedRequest(
                method=method,
                url=url,
                body=body,
                connect_timeout_s=connect_timeout_s,
                total_timeout_s=total_timeout_s,
            )
        )
        if not self.responses:
            raise AssertionError(f"unexpected request {method} {url}")
        return self.responses.pop(0)

    @property
    def count(self) -> int:
        return len(self.requests)

    def sent_texts(self, index: int) -> list[str]:
        """The address texts posted in request number `index`."""
        return json.loads(self.requests[index].body or b"[]")
