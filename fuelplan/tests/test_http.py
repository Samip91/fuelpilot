"""Spec 01 §8 #1: the shared transport's total deadline is hard, even for a dripping server."""

import http.client
import socket
import threading
import time
from unittest import mock

from fuelplan.http import HttpClientTransport, ResponseTooLarge, TransportError, TransportTimeout

from .base import LOOPBACK_HOST, NetworkAccessBlocked, OfflineTestCase

DEADLINE_S = 1.0
ALLOWED_OVERSHOOT_S = 0.5
DRIP_INTERVAL_S = 0.2
HEADERS = b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nContent-Type: text/plain\r\n\r\n"
# No Content-Length: the body ends when the server closes, so a shut-down socket
# looks like a normal end of body unless the transport checks its deadline flag.
WATCHDOG_EARLY_S = 0.3
CLOSE_DELIMITED_HEADERS = (
    b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: text/plain\r\n\r\n"
)


def _reply(body: bytes, *, status: str = "200 OK", extra: bytes = b"") -> bytes:
    return f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n".encode() + extra + b"\r\n" + body


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
        self.assertGreaterEqual(elapsed_s, DEADLINE_S - 0.1)

    def test_a_server_dripping_the_body_times_out_within_the_deadline(self) -> None:
        elapsed_s = self._call_drip_server(prefix=HEADERS, drip=b"x" * 1000)

        self.assertLessEqual(elapsed_s, DEADLINE_S + ALLOWED_OVERSHOOT_S)
        self.assertGreaterEqual(elapsed_s, DEADLINE_S - 0.1)

    def test_a_close_delimited_drip_cut_by_the_watchdog_is_a_timeout_not_a_body(self) -> None:
        # The watchdog fires early here, before any socket read timeout could: the cut
        # stream then ends like a normal close-delimited body, and only the transport's
        # "deadline passed" flag can tell the difference.
        real_timer = threading.Timer

        def early_timer(interval: float, function: object, args: tuple) -> threading.Timer:
            return real_timer(WATCHDOG_EARLY_S, function, args=args)

        with mock.patch("fuelplan.http.threading.Timer", side_effect=early_timer):
            elapsed_s = self._call_drip_server(prefix=CLOSE_DELIMITED_HEADERS, drip=b"x" * 1000)

        self.assertLess(elapsed_s, DEADLINE_S)

    def test_a_non_loopback_address_is_still_blocked(self) -> None:
        with self.assertRaises(NetworkAccessBlocked):
            HttpClientTransport().request(
                method="GET",
                url="http://10.255.255.1/",
                connect_timeout_s=1,
                total_timeout_s=1,
                max_bytes=10,
            )


class TransportLimitTests(OfflineTestCase):
    allow_loopback = True

    def _get(self, *, reply: bytes, max_bytes: int = 10_000):
        server = DripServer(prefix=reply, drip=b"")
        self.addCleanup(server.close)
        return HttpClientTransport().request(
            method="GET",
            url=f"http://{LOOPBACK_HOST}:{server.port}/route",
            connect_timeout_s=1,
            total_timeout_s=DEADLINE_S,
            max_bytes=max_bytes,
        )

    def test_a_body_of_exactly_max_bytes_is_read_and_one_more_byte_is_refused(self) -> None:
        response = self._get(reply=_reply(b"x" * 100), max_bytes=100)
        self.assertEqual(response.body, b"x" * 100)

        with self.assertRaises(ResponseTooLarge):
            self._get(reply=_reply(b"x" * 101), max_bytes=100)

    def test_a_redirect_is_returned_as_is_and_never_followed(self) -> None:
        response = self._get(
            reply=_reply(b"", status="302 Found", extra=b"Location: http://elsewhere.test/\r\n")
        )

        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["location"], "http://elsewhere.test/")

    def test_a_watchdog_firing_after_the_request_never_touches_the_socket(self) -> None:
        captured: list[tuple[object, tuple]] = []

        def fake_timer(interval: float, function: object, args: tuple) -> mock.Mock:
            captured.append((function, args))
            return mock.Mock()

        with mock.patch("fuelplan.http.threading.Timer", side_effect=fake_timer):
            self._get(reply=_reply(b"ok"))
        [(fire, args)] = captured

        with mock.patch.object(socket.socket, "shutdown") as shutdown:
            fire(*args)

        shutdown.assert_not_called()

    def test_the_connect_timeout_never_exceeds_the_total_deadline(self) -> None:
        connection = mock.Mock()
        connection.connect.side_effect = OSError("stop after the constructor")

        with (
            mock.patch.object(http.client, "HTTPConnection", return_value=connection) as cls,
            self.assertRaises(TransportError),
        ):
            HttpClientTransport().request(
                method="GET",
                url="http://osrm.test/route",
                connect_timeout_s=5,
                total_timeout_s=1,
                max_bytes=10,
            )

        self.assertEqual(cls.call_args.kwargs["timeout"], 1)


class SocketTimeoutTests(OfflineTestCase):
    """A socket `TimeoutError` at any step is a `TransportTimeout` (504), never a 502."""

    def _request_with(self, connection: mock.Mock) -> None:
        # The watchdog timer is faked so it can never fire on the mocked socket.
        with (
            mock.patch.object(http.client, "HTTPConnection", return_value=connection),
            mock.patch("fuelplan.http.threading.Timer"),
        ):
            HttpClientTransport().request(
                method="GET",
                url="http://osrm.test/route",
                connect_timeout_s=1,
                total_timeout_s=5,
                max_bytes=10,
            )

    def test_a_connect_that_times_out_is_a_transport_timeout(self) -> None:
        connection = mock.Mock()
        connection.connect.side_effect = TimeoutError("timed out")

        with self.assertRaises(TransportTimeout):
            self._request_with(connection)

    def test_a_response_that_times_out_before_the_watchdog_fires_is_a_transport_timeout(
        self,
    ) -> None:
        connection = mock.Mock()
        connection.getresponse.side_effect = TimeoutError("timed out")

        with self.assertRaises(TransportTimeout):
            self._request_with(connection)

        connection.request.assert_called_once()  # the request went out; the reply was late


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
