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
import io
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

from open_sstv.remote.compose import ComposeService
from open_sstv.remote.control import ControlPlane, Result
from open_sstv.remote.events import EventHub
from open_sstv.remote.page import render_page

if TYPE_CHECKING:
    from collections.abc import Callable

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
#: How often the control-plane safety sweep runs.  Must be well under
#: ``control.HEARTBEAT_TIMEOUT_S`` (2.5 s) so the dead-man's-switch fires
#: promptly after a lost heartbeat.
_TICK_INTERVAL_S = 0.5
#: Control-plane POST bodies are tiny JSON; reject anything larger.
_MAX_BODY_BYTES = 64 * 1024
#: Compose uploads carry a base64 photo — allow more (base64 inflates the
#: 12 MB photo cap by ~⅓).
_MAX_UPLOAD_BYTES = 18 * 1024 * 1024
#: Map a ControlPlane ``Result.error`` code to an HTTP status.
_ERR_STATUS = {
    "busy": HTTPStatus.CONFLICT,
    "not_lease_holder": HTTPStatus.FORBIDDEN,
    "tx_disabled": HTTPStatus.FORBIDDEN,
    "no_rig": HTTPStatus.CONFLICT,
    "bad_token": HTTPStatus.BAD_REQUEST,
    "confirm_expired": HTTPStatus.CONFLICT,
}
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


def _is_known_mode(mode: str) -> bool:
    """True if *mode* is a valid ``core.modes.Mode`` value.

    Lazy import so this module doesn't pull the DSP stack for callers that
    never transmit; if it can't import, don't block here — the transmit
    path validates again downstream.
    """
    try:
        from open_sstv.core.modes import Mode  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return True
    return mode in {m.value for m in Mode}


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
        control: ControlPlane | None = None,
        tx_enabled: Callable[[], bool] | None = None,
        compose: ComposeService | None = None,
    ) -> None:
        self._service = service
        #: Server-side compositor (Phase 4).  Shares the gallery service's
        #: live config so it renders with the operator's callsign/grid.
        self._compose = compose if compose is not None else ComposeService(
            service.config_getter
        )
        self._host = host
        self._port = port
        #: Remote-TX control plane (Phase 3).  Defaults to one whose enable
        #: gate is ``tx_enabled`` (or hard-off) with **stub** transmit/unkey
        #: callbacks that only log — the server never touches the rig by
        #: itself.  The app (3c) passes a ControlPlane with real callbacks.
        #: A dedicated tick thread drives the dead-man's-switch, independent
        #: of any GUI event loop.
        self._tx_enabled = tx_enabled or (lambda: False)
        self._control = control if control is not None else ControlPlane(
            now=time.monotonic,
            transmit=self._stub_transmit,
            unkey=self._stub_unkey,
            enabled=self._tx_enabled,
        )
        self._tick_thread: threading.Thread | None = None
        #: Never run unprotected: mint a token if the caller gave none.
        self._token = token or secrets.token_urlsafe(16)
        #: Live event fan-out for the SSE stream.  The app publishes RX
        #: progress / gallery updates here; ``/api/events`` drains it.
        self._hub = hub if hub is not None else EventHub()
        #: Set on stop() so in-flight SSE readers exit promptly.
        self._stopping = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- default (no-rig) control callbacks ----------------------------
    # Used only when no ControlPlane is injected (Phase 3b / standalone /
    # tests).  They NEVER touch a rig — the server cannot transmit by
    # itself; the app supplies real callbacks in 3c.

    def _stub_transmit(self, image_id: str, mode: str) -> None:
        _log.info("remote TX (stub — no rig): would transmit %s in %s", image_id, mode)

    def _stub_unkey(self, reason: str) -> None:
        _log.info("remote TX (stub — no rig): would unkey (%s)", reason)

    @property
    def control(self) -> ControlPlane:
        return self._control

    def _publish_tx_state(self) -> None:
        """Fan the current control-plane state out to SSE viewers."""
        self._hub.publish({"type": "tx.state", **self._control.status()})

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
        hub, stopping, control = self._hub, self._stopping, self._control
        compose = self._compose

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

                if path == "/api/compose/templates":
                    self._json(HTTPStatus.OK, compose.list_templates())
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

            # -- control plane (POST) ---------------------------------
            # State-changing requests: token + same-origin + JSON body with
            # a per-browser ``client`` id.  Every route maps a ControlPlane
            # Result to an HTTP status and fans the new state out over SSE.

            def _origin_ok(self) -> bool:
                """Reject cross-origin POSTs (CSRF).  A browser always sends
                Origin on a POST; a same-origin one matches Host.  Non-browser
                clients (curl/tests) omit it and are allowed — the token still
                gates them."""
                origin = self.headers.get("Origin")
                if origin is None:
                    return True
                netloc = origin.split("://", 1)[-1] if "://" in origin else ""
                return netloc == self.headers.get("Host", "")

            def _read_json(
                self, max_bytes: int = _MAX_BODY_BYTES
            ) -> dict[str, object] | None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return None
                if length <= 0 or length > max_bytes:
                    return None
                try:
                    obj = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError):
                    return None
                return obj if isinstance(obj, dict) else None

            def _do_compose(self) -> None:
                """POST /api/compose/render — render photo+tokens+template to
                a PNG preview.  Composing never touches the rig; putting the
                result on air still goes through the control plane."""
                import base64  # noqa: PLC0415

                body = self._read_json(_MAX_UPLOAD_BYTES)
                if body is None:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid or missing body"})
                    return
                photo_b64 = body.get("photo")
                template_id = body.get("template_id")
                mode = body.get("mode")
                tokens = body.get("tokens")
                if not isinstance(photo_b64, str) or not isinstance(template_id, str) \
                        or not isinstance(mode, str):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "missing photo/template_id/mode"})
                    return
                try:
                    photo = base64.b64decode(photo_b64, validate=True)
                except ValueError:  # binascii.Error subclasses ValueError
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "bad base64 photo"})
                    return
                tok = {k: str(v) for k, v in tokens.items()} if isinstance(tokens, dict) else {}
                img = compose.render(photo, template_id, tok, mode)
                if img is None:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "could not render"})
                    return
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "PNG")
                self._send(HTTPStatus.OK, buf.getvalue(), "image/png")

            def _do_stage(self) -> None:
                """POST /api/compose/stage — render + hold the image in memory
                for transmit, returning a staging id.  Gated on remote TX
                being enabled (staging is only useful to then transmit)."""
                import base64  # noqa: PLC0415

                if not control.status().get("tx_enabled"):
                    self._json(HTTPStatus.FORBIDDEN, {"error": "tx_disabled"})
                    return
                body = self._read_json(_MAX_UPLOAD_BYTES)
                if body is None:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid or missing body"})
                    return
                photo_b64 = body.get("photo")
                template_id = body.get("template_id")
                mode = body.get("mode")
                tokens = body.get("tokens")
                if not isinstance(photo_b64, str) or not isinstance(template_id, str) \
                        or not isinstance(mode, str):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "missing photo/template_id/mode"})
                    return
                try:
                    photo = base64.b64decode(photo_b64, validate=True)
                except ValueError:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "bad base64 photo"})
                    return
                tok = {k: str(v) for k, v in tokens.items()} if isinstance(tokens, dict) else {}
                staging_id = compose.stage(photo, template_id, tok, mode)
                if staging_id is None:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "could not render"})
                    return
                self._json(HTTPStatus.OK, {"image_id": staging_id})

            def _tx_result(self, result: Result, extra: dict[str, object] | None = None) -> None:
                # Fan the new control state to viewers, then answer the caller.
                hub.publish({"type": "tx.state", **control.status()})
                if result.ok:
                    body: dict[str, object] = {"ok": True, **control.status()}
                    if extra:
                        body.update(extra)
                    self._json(HTTPStatus.OK, body)
                else:
                    status = _ERR_STATUS.get(result.error or "", HTTPStatus.BAD_REQUEST)
                    self._json(
                        status, {"ok": False, "error": result.error, **control.status()}
                    )

            def do_POST(self) -> None:  # noqa: N802 — stdlib API
                parts = urlsplit(self.path)
                path, query = parts.path, parse_qs(parts.query)
                if path not in ("/api/compose/render", "/api/compose/stage") \
                        and not path.startswith("/api/tx/"):
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                if not self._authed(query):
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid or missing token"})
                    return
                if not self._origin_ok():
                    self._json(HTTPStatus.FORBIDDEN, {"error": "cross-origin refused"})
                    return
                if path == "/api/compose/render":
                    self._do_compose()
                    return
                if path == "/api/compose/stage":
                    self._do_stage()
                    return
                body = self._read_json()
                if body is None:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid or missing body"})
                    return
                client = body.get("client")
                if not isinstance(client, str) or not client:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "missing client id"})
                    return

                if path == "/api/tx/lease":
                    self._tx_result(control.take_lease(client))
                elif path == "/api/tx/release":
                    self._tx_result(control.release_lease(client))
                elif path == "/api/tx/heartbeat":
                    self._tx_result(control.heartbeat(client))
                elif path == "/api/tx/abort":
                    self._tx_result(control.abort(client))
                elif path == "/api/tx/confirm":
                    tok = body.get("token")
                    if not isinstance(tok, str):
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "missing token"})
                        return
                    self._tx_result(control.confirm(client, tok))
                elif path == "/api/tx/request":
                    image_id, mode = body.get("image_id"), body.get("mode")
                    if not isinstance(image_id, str) or not isinstance(mode, str):
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "missing image_id/mode"})
                        return
                    if not _is_known_mode(mode):
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "unknown mode"})
                        return
                    # The image must exist: a staged compose (in memory) or a
                    # gallery file (on disk).
                    known = (
                        compose.staged_image(image_id) is not None
                        if compose.is_staged_id(image_id)
                        else service.image_path(image_id) is not None
                    )
                    if not known:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "unknown image"})
                        return
                    result = control.request(client, image_id, mode)
                    self._tx_result(result, {"token": result.token} if result.token else None)
                else:
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
                    # EventSource fires `onopen` immediately; follow it with
                    # the current TX state so the control UI initialises
                    # without waiting for the next state change.
                    self.wfile.write(b": connected\n\n")
                    init = json.dumps({"type": "tx.state", **control.status()})
                    self.wfile.write(b"data: " + init.encode("utf-8") + b"\n\n")
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
        # Drive the control-plane safety sweep (dead-man's-switch, token
        # expiry, lease lapse) on a dedicated thread — deliberately NOT the
        # GUI loop, so a stalled UI can't stop the watchdog from unkeying.
        self._tick_thread = threading.Thread(
            target=self._tick_loop, name="open-sstv-remote-tick", daemon=True
        )
        self._tick_thread.start()
        # NB: log the bind, never ``self.url`` — the URL embeds the token,
        # and the log file rides along in diagnostics exports.
        _log.info("remote gallery server listening on %s:%d", self._host, self._port)

    def _tick_loop(self) -> None:
        while not self._stopping.wait(_TICK_INTERVAL_S):
            try:
                self._control.tick()
            except Exception:  # noqa: BLE001 — the watchdog must never die
                _log.exception("remote control tick failed")

    def stop(self) -> None:
        """Stop serving and release the socket.  Safe to call when not running."""
        httpd, thread, ticker = self._httpd, self._thread, self._tick_thread
        self._httpd = self._thread = self._tick_thread = None
        # Signal SSE readers + the tick loop first so they unwind promptly.
        self._stopping.set()
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=5.0)
        if ticker is not None:
            ticker.join(timeout=2.0)

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
