"""A small HTTP transport with hard deadlines, used for every outbound call (spec 01 §8)."""

import contextlib
import http.client
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

READ_CHUNK_BYTES = 64 * 1024
ALLOWED_SCHEMES = frozenset({"http", "https"})


class TransportError(Exception):
    """The request could not be completed (connection, DNS, TLS or protocol error)."""


class TransportTimeout(TransportError):
    """Connecting or reading the full response took longer than allowed."""


class ResponseTooLarge(TransportError):
    """The response body was larger than the caller allows."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Status, lower-cased headers and the full body of one HTTP response."""

    status: int
    headers: dict[str, str]
    body: bytes


class Transport(Protocol):
    """Anything that can send one HTTP request; tests use a counting fake."""

    def request(
        self,
        *,
        method: str,
        url: str,
        connect_timeout_s: float,
        total_timeout_s: float,
        max_bytes: int,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse: ...


_API_KEY_PATTERN = re.compile(r"(apiKey=)[^&]+", re.IGNORECASE)


def redact_url(url: str) -> str:
    """Hide API keys before a URL is logged or put into an error message."""
    return _API_KEY_PATTERN.sub(r"\1***", url)


def require_http_base_url(url: str) -> str:
    """A configured base URL, checked once: http(s), a host, no query, fragment or user info.

    Returns it without a trailing "/". The error never echoes the URL, because a bad
    one may carry a username or password.
    """
    try:
        parts = urlsplit(url.strip())
        hostname = parts.hostname
        _ = parts.port  # raises ValueError for a port that is not a number
    except ValueError:
        raise ValueError("must be an http(s) URL with a host") from None
    if parts.scheme not in ALLOWED_SCHEMES or not hostname:
        raise ValueError("must be an http(s) URL with a host")
    # Checked on the text: "https://x/?" has an empty query but still a "?".
    if "?" in url or "#" in url or "@" in parts.netloc:
        raise ValueError("must not contain a query, fragment, username or password")
    return url.strip().rstrip("/")


class _Watchdog:
    """Shuts the socket down when the total deadline passes, unless the request is over.

    The lock and `closed` flag make "finish the request" and "fire" exclusive, so a
    timer that fires late can never shut down a socket number that was reused.
    """

    def __init__(self, sock: socket.socket, *, after_s: float) -> None:
        self.expired = threading.Event()
        self._lock = threading.Lock()
        self._closed = False
        self._timer = threading.Timer(after_s, self._fire, args=(sock,))
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def close(self) -> None:
        """Called when the request ends; after this the watchdog never touches the socket."""
        with self._lock:
            self._closed = True
        self._timer.cancel()

    def _fire(self, sock: socket.socket) -> None:
        with self._lock:
            if self._closed:
                return
            self.expired.set()
            # The plain socket shutdown (not SSLSocket's override) ends the stream without
            # touching TLS state that the reading thread is still using.
            with contextlib.suppress(OSError):  # the peer already closed the connection
                socket.socket.shutdown(sock, socket.SHUT_RDWR)


class HttpClientTransport:
    """Standard-library transport: connect deadline, hard total deadline, size cap.

    The total deadline is hard: a watchdog timer shuts the socket down when it
    passes, so a server that drips one byte at a time cannot stretch the call.
    Redirects are never followed, so a 3xx cannot move a request to another host.
    TCP and TLS setup are bounded by the connect timeout (never above the total one);
    DNS resolution happens inside `connect()` and is not bounded by either deadline.
    """

    def request(
        self,
        *,
        method: str,
        url: str,
        connect_timeout_s: float,
        total_timeout_s: float,
        max_bytes: int,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        parts = urlsplit(url)
        if parts.scheme not in ALLOWED_SCHEMES or not parts.hostname:
            raise TransportError(f"refusing to call {redact_url(url)}: not an http(s) URL")
        connection_class = (
            http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_class(
            parts.hostname, parts.port, timeout=min(connect_timeout_s, total_timeout_s)
        )
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"
        deadline = time.monotonic() + total_timeout_s
        watchdog: _Watchdog | None = None
        try:
            connection.connect()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or connection.sock is None:
                raise TransportTimeout(f"timed out connecting to {redact_url(url)}")
            watchdog = _Watchdog(connection.sock, after_s=remaining)
            watchdog.start()
            connection.sock.settimeout(remaining)
            connection.request(method, target, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = self._read_body(connection, response, deadline=deadline, max_bytes=max_bytes)
            if watchdog.expired.is_set():  # a shut-down socket can look like a normal end
                raise TransportTimeout("response not complete before the deadline")
            return HttpResponse(
                status=response.status,
                headers={name.lower(): value for name, value in response.getheaders()},
                body=payload,
            )
        except TimeoutError as error:
            raise TransportTimeout(f"timed out calling {redact_url(url)}") from error
        except (OSError, http.client.HTTPException) as error:
            if watchdog is not None and watchdog.expired.is_set():
                raise TransportTimeout(
                    f"response from {redact_url(url)} not complete before the deadline"
                ) from error
            raise TransportError(f"failed calling {redact_url(url)}: {error}") from error
        finally:
            if watchdog is not None:
                watchdog.close()
            connection.close()

    @staticmethod
    def _read_body(
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
        *,
        deadline: float,
        max_bytes: int,
    ) -> bytes:
        """Read the body in chunks, keeping each read inside the remaining time."""
        chunks: list[bytes] = []
        received = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportTimeout("response not complete before the deadline")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read(READ_CHUNK_BYTES)
            if not chunk:
                return b"".join(chunks)
            received += len(chunk)
            if received > max_bytes:
                raise ResponseTooLarge(f"response body exceeds {max_bytes} bytes")
            chunks.append(chunk)
