# SPDX-License-Identifier: GPL-3.0-or-later
"""Embedded read-only HTTP server for the remote gallery (Phase 1).

Wraps a stdlib :class:`~http.server.ThreadingHTTPServer` in a daemon
thread so it runs alongside the Qt event loop without blocking it — the
whole point of the Phase 1 spike is to prove this coexistence.  The
handler is deliberately tiny:

    GET /                     → the read-only viewer page (no token needed
                                to load the shell; it needs the token to
                                call the APIs below)
    GET /api/gallery          → JSON list of images
    GET /api/thumb/<id>       → cached thumbnail PNG
    GET /api/image/<id>       → original image bytes

Security posture for this phase (a LAN dev-token surface, no TX):

- **Bearer/query token on every ``/api`` call**, compared with
  :func:`hmac.compare_digest`.  A random token is generated if the caller
  supplies none, so the server is never unprotected.
- **Images are addressed by opaque id only** — the id→path mapping lives
  in :class:`~open_sstv.remote.service.GalleryService` and only ever
  resolves to files inside the configured gallery directories, so no
  client-supplied path ever reaches the filesystem.
- **Read-only.**  Only ``GET`` is handled; there is no route that writes,
  deletes, or transmits.

Phase 2 adds the live view plane on the same stdlib server: ``GET
/api/events`` is a **Server-Sent Events** stream fed by
:class:`~open_sstv.remote.events.EventHub`, and ``GET /api/rx/preview``
serves the current in-progress decode.  The view plane is one-directional
(app→browser), so SSE fits without an async framework or WebSockets —
stdlib only, still no new dependency.
"""
from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import queue
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from open_sstv.remote.events import EventHub
from open_sstv.remote.page import render_page

if TYPE_CHECKING:
    from open_sstv.remote.service import GalleryService

#: Seconds a stalled SSE reader waits before re-checking the stop flag.
#: Short so :meth:`RemoteServer.stop` returns promptly; the wait is idle
#: (blocked on a queue) so a low value costs nothing.
_SSE_POLL_S = 1.0
#: Comment-line keep-alive cadence — proves the connection to the client
#: (and surfaces a dead socket to us) between real events.
_SSE_PING_S = 15.0
#: Ceiling on simultaneous SSE streams (each parks a request thread).
#: A home station has a handful of viewers; this bounds thread/memory use
#: if a client reconnect-storms or many tabs pile up.
_MAX_SSE_CLIENTS = 16
#: Content Security Policy.  The page is inline-everything, so script/style
#: need ``'unsafe-inline'`` — but ``default-src 'none'`` + ``connect-src
#: 'self'`` mean that even if markup ever slips past escaping, it can't
#: phone the token home to another origin or pull external resources.
_CSP = (
    "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
    "form-action 'none'"
)

_log = logging.getLogger(__name__)


class RemoteServer:
    """Owns the embedded HTTP server's lifecycle.

    Construct, :meth:`start`, and later :meth:`stop`.  ``start`` binds the
    socket (raising :class:`OSError` on e.g. a port already in use, for
    the caller to surface) and then serves on a background daemon thread.
    """

    def __init__(
        self,
        service: GalleryService,
        host: str = "127.0.0.1",
        port: int = 8730,
        token: str = "",
        hub: EventHub | None = None,
    ) -> None:
        self._service = service
        self._host = host
        self._port = port
        #: Never run unprotected: mint a token if the caller gave none.
        self._token = token or secrets.token_urlsafe(16)
        #: Live event fan-out for the SSE stream.  The app publishes RX
        #: progress / gallery updates here; ``/api/events`` drains it.
        self._hub = hub if hub is not None else EventHub()
        #: Set on stop() so in-flight SSE readers exit promptly.
        self._stopping = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Bind the socket and start serving on a daemon thread.

        Idempotent-ish: a second call while running is a no-op.  Binding
        errors propagate so the caller can log and carry on without the
        server (never blocking app launch).
        """
        if self._httpd is not None:
            return
        self._stopping.clear()
        service, token = self._service, self._token
        hub, stopping = self._hub, self._stopping

        class _Handler(BaseHTTPRequestHandler):
            # Route stdlib access logs through our logger at DEBUG rather
            # than spamming stderr with one line per request.
            def log_message(self, fmt: str, *args: object) -> None:
                _log.debug("remote %s - %s", self.address_string(), fmt % args)

            def _authed(self, query: dict[str, list[str]]) -> bool:
                supplied = ""
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Bearer "):
                    supplied = auth[7:]
                elif "token" in query:
                    supplied = query["token"][0]
                return hmac.compare_digest(supplied, token)

            def _send(self, status: HTTPStatus, body: bytes, ctype: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                # Belt-and-braces: this surface never wants to be framed
                # or sniffed, and it must not be cached with its token.
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", _CSP)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, status: HTTPStatus, obj: object) -> None:
                self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

            def _file(self, path_str: str, ctype: str) -> None:
                # ``path_str`` came from GalleryService.resolve — it is a
                # known gallery file, not client input.
                try:
                    with open(path_str, "rb") as fh:  # noqa: PTH123 — bytes read
                        data = fh.read()
                except OSError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                self._send(HTTPStatus.OK, data, ctype)

            def do_GET(self) -> None:  # noqa: N802 — stdlib API
                parts = urlsplit(self.path)
                path = parts.path
                query = parse_qs(parts.query)

                if path in ("/", "/index.html"):
                    self._send(
                        HTTPStatus.OK,
                        render_page().encode("utf-8"),
                        "text/html; charset=utf-8",
                    )
                    return

                if not path.startswith("/api/"):
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return

                if not self._authed(query):
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid or missing token"})
                    return

                if path == "/api/gallery":
                    self._json(HTTPStatus.OK, service.payload())
                    return

                if path == "/api/logbook":
                    self._json(HTTPStatus.OK, service.logbook_payload())
                    return

                if path == "/api/events":
                    self._sse()
                    return

                if path == "/api/rx/preview":
                    png = service.live_frame()
                    if png is None:
                        self._send(HTTPStatus.NO_CONTENT, b"", "image/png")
                        return
                    self._send(HTTPStatus.OK, png, "image/png")
                    return

                if path.startswith("/api/thumb/"):
                    thumb = service.thumbnail_path(path[len("/api/thumb/"):])
                    if thumb is None:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "unknown image"})
                        return
                    self._file(str(thumb), "image/png")
                    return

                if path.startswith("/api/image/"):
                    img = service.image_path(path[len("/api/image/"):])
                    if img is None:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "unknown image"})
                        return
                    ctype = mimetypes.guess_type(img.name)[0] or "application/octet-stream"
                    self._file(str(img), ctype)
                    return

                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def _sse(self) -> None:
                """Stream live events to the client until it (or we) hang up."""
                # Bound concurrent streams so a reconnect-storm or a pile of
                # tabs can't exhaust threads/memory on the host.
                q = hub.subscribe(max_subscribers=_MAX_SSE_CLIENTS)
                if q is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "too many clients"})
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", _CSP)
                self.send_header("X-Accel-Buffering", "no")  # defeat proxy buffering
                self.end_headers()
                last_ping = time.monotonic()
                try:
                    # A first comment flushes headers so the browser's
                    # EventSource fires `onopen` immediately.
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    while not stopping.is_set():
                        try:
                            event = q.get(timeout=_SSE_POLL_S)
                        except queue.Empty:
                            if time.monotonic() - last_ping >= _SSE_PING_S:
                                self.wfile.write(b": ping\n\n")
                                self.wfile.flush()
                                last_ping = time.monotonic()
                            continue
                        payload = json.dumps(event).encode("utf-8")
                        self.wfile.write(b"data: " + payload + b"\n\n")
                        self.wfile.flush()
                        last_ping = time.monotonic()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # client went away — normal
                finally:
                    hub.unsubscribe(q)

        httpd = ThreadingHTTPServer((self._host, self._port), _Handler)
        httpd.daemon_threads = True  # request threads never block shutdown
        self._httpd = httpd
        # Reflect the actually-bound port (matters when port==0 was passed).
        self._port = httpd.server_address[1]
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            name="open-sstv-remote",
            daemon=True,
        )
        self._thread.start()
        # NB: log the bind, never ``self.url`` — the URL embeds the token,
        # and the log file rides along in diagnostics exports.
        _log.info("remote gallery server listening on %s:%d", self._host, self._port)

    def stop(self) -> None:
        """Stop serving and release the socket.  Safe to call when not running."""
        httpd, thread = self._httpd, self._thread
        self._httpd = self._thread = None
        # Signal SSE readers first so they unwind out of their queue wait
        # instead of lingering until the next keep-alive write.
        self._stopping.set()
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=5.0)

    # -- introspection -------------------------------------------------

    @property
    def hub(self) -> EventHub:
        """The event fan-out the app publishes live RX events onto."""
        return self._hub

    @property
    def is_running(self) -> bool:
        return self._httpd is not None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        """The bound port (resolved after :meth:`start` when port 0 was used)."""
        return self._port

    @property
    def token(self) -> str:
        return self._token

    @property
    def url(self) -> str:
        """The full URL an operator opens, token included."""
        return f"http://{self._host}:{self._port}/?token={self._token}"


__all__ = ["RemoteServer"]
