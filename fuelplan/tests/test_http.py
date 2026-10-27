"""Spec 01 §8 #1: the shared transport's total deadline is hard, even for a dripping server."""

import socket
import threading
import time

from fuelplan.http import HttpClientTransport, TransportError, TransportTimeout

from .base import LOOPBACK_HOST, NetworkAccessBlocked, OfflineTestCase

DEADLINE_S = 1.0
ALLOWED_OVERSHOOT_S = 0.5
DRIP_INTERVAL_S = 0.2
HEADERS = b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nContent-Type: text/plain\r\n\r\n"


class DripServer:
    """One-connection server on 127.0.0.1 that sends its reply one byte at a time."""

    def __init__(self, *, prefix: bytes, drip: bytes) -> None:
        self._prefix = prefix
        self._drip = drip
        self._stop = threading.Event()
        self._listener = socket.create_server((LOOPBACK_HOST, 0))
        self._listener.settimeout(5)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            client, _address = self._listener.accept()
        except TimeoutError:
            return
        with client:
            client.recv(65536)  # the request; its content does not matter here
            try:
                client.sendall(self._prefix)
                for byte in self._drip:
                    if self._stop.wait(DRIP_INTERVAL_S):
                        return
                    client.sendall(bytes([byte]))
            except OSError:
                return  # the client hung up, which is what the deadline should do

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._listener.close()


class HardDeadlineTests(OfflineTestCase):
    allow_loopback = True

    def _call_drip_server(self, *, prefix: bytes, drip: bytes) -> float:
        server = DripServer(prefix=prefix, drip=drip)
        self.addCleanup(server.close)
        started = time.monotonic()
        with self.assertRaises(TransportTimeout):
            HttpClientTransport().request(
                method="GET",
                url=f"http://{LOOPBACK_HOST}:{server.port}/route",
                connect_timeout_s=1,
                total_timeout_s=DEADLINE_S,
                max_bytes=10_000,
            )
        return time.monotonic() - started

    def test_a_server_dripping_the_headers_times_out_within_the_deadline(self) -> None:
        elapsed_s = self._call_drip_server(prefix=b"", drip=HEADERS)

        self.assertLessEqual(elapsed_s, DEADLINE_S + ALLOWED_OVERSHOOT_S)

    def test_a_server_dripping_the_body_times_out_within_the_deadline(self) -> None:
        elapsed_s = self._call_drip_server(prefix=HEADERS, drip=b"x" * 1000)

        self.assertLessEqual(elapsed_s, DEADLINE_S + ALLOWED_OVERSHOOT_S)

    def test_a_non_loopback_address_is_still_blocked(self) -> None:
        with self.assertRaises(NetworkAccessBlocked):
            HttpClientTransport().request(
                method="GET",
                url="http://10.255.255.1/",
                connect_timeout_s=1,
                total_timeout_s=1,
                max_bytes=10,
            )


class UrlCheckTests(OfflineTestCase):
    def test_schemes_other_than_http_and_https_are_refused(self) -> None:
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/"):
            with self.subTest(url=url), self.assertRaisesMessage(TransportError, "not an http"):
                HttpClientTransport().request(
                    method="GET", url=url, connect_timeout_s=1, total_timeout_s=1, max_bytes=10
                )

    def test_a_url_without_a_hostname_is_refused(self) -> None:
        with self.assertRaisesMessage(TransportError, "not an http"):
            HttpClientTransport().request(
                method="GET",
                url="http:///path",
                connect_timeout_s=1,
                total_timeout_s=1,
                max_bytes=10,
            )
